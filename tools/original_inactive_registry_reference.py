"""Observe the pinned inactive predecessor using real on-disk registries."""

from __future__ import annotations

import copy
import hashlib
import inspect
import os
import shutil
import stat
import sys
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from types import FrameType, SimpleNamespace
from typing import Any
from unittest.mock import patch

from original_authority_reference import ORIGINAL_HEAD, error_graph, write_json
from original_setup_candidate_selection_reference import _lock_free

FORMAT = "io.tobkiri.original-inactive-registry-reference.v1"
CASES = (
    "missing-row",
    "exact-mapping-equal",
    "tombstone",
    "different-row-no-handover",
    "authenticated-handover",
)
PREDECESSOR_SOURCE = "core_runtime/bootstrap/profile_predecessor.py"
ACTIVE_SOURCE = "core_runtime/active_profile_store_v4.py"
HANDOVER_SOURCE = "core_runtime/development_host_handover_guard.py"
SANITIZER_SOURCE = "ecosystem/defaultspack/defaultspack/development_host_data.py"
PERSISTENCE_SOURCE = "tobkiri_protocol/secure_persistence.py"
STORE_SOURCE = "core_runtime/profile_definition_store_v4.py"
SETUP_RUNTIME_SOURCE = "ecosystem/defaultspack/defaultspack/profile_runtime_composition.py"
CAPTURE_SOURCE = "core_runtime/bootstrap/profile_capture.py"
PUBLICATION_SOURCE = "core_runtime/bootstrap/profile_publication.py"
SOURCES = (
    PREDECESSOR_SOURCE,
    ACTIVE_SOURCE,
    HANDOVER_SOURCE,
    SANITIZER_SOURCE,
    PERSISTENCE_SOURCE,
    STORE_SOURCE,
    SETUP_RUNTIME_SOURCE,
    CAPTURE_SOURCE,
    PUBLICATION_SOURCE,
)
LOCK = "profiles/.profile-definition.lock"
INDEX = "profiles/index.json"
JOURNAL = "development-host-handover.json"
KEY = "development-host-handover.key"
REGISTRATION_TIME = 1_700_000_123


def _inventory(root: Path, names: list[str]) -> dict[str, tuple]:
    paths = sorted(root.rglob("*"))
    actual = [path.relative_to(root).as_posix() for path in paths]
    if actual != sorted(["profiles", *names]):
        raise AssertionError(("unexpected original fixture entries", actual))
    result = {}
    for name, path in [(".", root), *zip(actual, paths)]:
        info = path.lstat()
        directory = name in {".", "profiles"}
        if directory:
            if not stat.S_ISDIR(info.st_mode):
                raise AssertionError(("expected a real directory", name))
        elif (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or info.st_size > 1 << 20
        ):
            raise AssertionError(("expected a bounded regular file", name))
        result[name] = (
            info.st_dev,
            info.st_ino,
            info.st_mode,
            info.st_mtime_ns,
            None if directory else path.read_bytes(),
        )
    return result


class _Observation:
    def __init__(self, runtime: Path, root: Path, source: Mapping[str, Any]) -> None:
        from core_runtime import active_profile_store_v4 as active
        from core_runtime import development_host_handover_guard as handover
        from core_runtime.bootstrap import profile_predecessor as predecessor
        from tobkiri_protocol.secure_persistence import SecureDirectory

        store = predecessor.ProfileDefinitionStore
        self.root = root
        self.source = source
        self.helper = predecessor.inactive_bootstrap_predecessor
        self.inactive = self.helper.__code__
        self.require_inactive = predecessor._require_inactive.__code__
        self.constructor = store.__init__.__code__
        self.snapshot = store.snapshot.__code__
        self.read_state = store._read_state.__code__
        self.lock = inspect.unwrap(active.exclusive_profile_lock).__code__
        self.handover = predecessor.read_handover_journal.__code__
        self.storage = SecureDirectory.__init__.__code__
        self.read = SecureDirectory.read_bytes_bounded.__code__
        self.read_wrapper = SecureDirectory.read_bytes.__code__
        self.forbidden = {
            store._write_state.__code__,
            handover.write_handover_journal.__code__,
            SecureDirectory.write_bytes_atomic.__code__,
        }
        sources = {
            PREDECESSOR_SOURCE: (self.inactive, self.require_inactive),
            ACTIVE_SOURCE: (self.lock,),
            HANDOVER_SOURCE: (self.handover,),
            PERSISTENCE_SOURCE: (self.storage, self.read, self.read_wrapper),
            STORE_SOURCE: (self.constructor, self.snapshot, self.read_state),
        }
        for relative, codes in sources.items():
            if any(
                Path(code.co_filename).resolve() != runtime / relative for code in codes
            ):
                raise AssertionError(
                    "inactive predecessor callable outside staged original"
                )
        self.calls: list[str] = []
        self.path_checks: list[list[str | bool]] = []
        self.instance: Any = None
        self.journal_storage: Any = None
        self.registry: Any = None
        self.journal: Any = None
        self.failure: BaseException | None = None
        self.path_operations = {
            Path.exists.__code__: "exists",
            Path.is_symlink.__code__: "is-symlink",
        }
        self.traced = (
            self.path_operations.keys()
            | self.forbidden
            | {code for codes in sources.values() for code in codes}
        )

    def trace(self, frame: FrameType, event: str, value: Any) -> Any:
        code = frame.f_code
        if code not in self.traced:
            return None
        parent, local = frame.f_back, frame.f_locals
        owner = None if parent is None else parent.f_code
        if event == "call" and code in self.forbidden:
            raise AssertionError("inactive registry lookup attempted a write")
        if len(self.calls) > 20 or len(self.path_checks) > 24:
            raise AssertionError("inactive registry observation exceeds its bound")
        if code in self.path_operations:
            if owner is not None and owner.co_name == "<genexpr>":
                owner = None if parent.f_back is None else parent.f_back.f_code
            if owner not in (
                self.inactive,
                self.require_inactive,
                self.constructor,
                self.handover,
            ):
                return None
            if event == "return":
                path = local["self"].relative_to(self.root).as_posix()
                operation = self.path_operations[code]
                if type(value) is not bool:
                    raise AssertionError((operation, path, value))
                self.path_checks.append([operation, path, value])
        elif code is self.inactive:
            if event == "call":
                if (
                    local["user_data"] is not self.root
                    or local["source"] is not self.source
                    or local["snapshot"] is not None
                ):
                    raise AssertionError(
                        "inactive predecessor did not load its own registry"
                    )
                self.calls.append("call-inactive-predecessor")
            elif event == "exception":
                self.failure = value[1]
            elif event == "return":
                finish = (
                    "error"
                    if self.failure is not None
                    else "none" if value is None else "binding"
                )
                self.calls.append(f"finish-inactive-predecessor-{finish}")
        elif code is self.constructor and owner is self.inactive:
            if event == "call":
                self.instance = local["self"]
                if local["user_data_root"] is not self.root:
                    raise AssertionError(
                        "inactive predecessor changed the registry root"
                    )
                self.calls.append("call-registry-constructor")
            elif event == "return":
                if self.instance.index_path != self.root / INDEX:
                    raise AssertionError("inactive predecessor changed the index path")
                self.calls.append("return-registry-constructor")
        elif code is self.storage and event == "call":
            if owner is self.constructor:
                if (
                    local["root"] != self.root / "profiles"
                    or local["create"] is not True
                ):
                    raise AssertionError(
                        "registry constructor changed its persistence scope"
                    )
            elif owner is self.handover:
                if local["root"] is not self.root or local["create"] is not False:
                    raise AssertionError("handover read changed its persistence scope")
                self.journal_storage = local["self"]
        elif code is self.snapshot and owner is self.inactive:
            if local["self"] is not self.instance:
                raise AssertionError("inactive predecessor used a different registry")
            if event == "call":
                self.calls.append("call-registry-snapshot")
            elif event == "return":
                self.registry = value
                self.calls.append("return-registry-snapshot")
        elif code is self.lock:
            if event == "return":
                acquired = self.calls[-1] == "call-registry-snapshot"
                if (
                    local["directory"] is not self.instance._directory
                    or local["relative_path"] != Path(LOCK).name
                    or local["acquired"] is not True
                    or local["process_lock"]._is_owned() != acquired
                    or _lock_free(self.root / LOCK) is not (not acquired)
                ):
                    raise AssertionError("original registry lock lifetime changed")
                action = "acquire" if acquired else "release"
                self.calls.append(f"{action}-registry-lock")
        elif code is self.read and event == "call":
            args = (local["self"], local["relative"], local["max_bytes"])
            if owner is self.read_wrapper:
                if (
                    parent.f_back is None
                    or parent.f_back.f_code is not self.read_state
                    or args != (self.instance._directory, Path(INDEX).name, None)
                ):
                    raise AssertionError(
                        "inactive predecessor changed its registry read"
                    )
                self.calls.append("read-registry-index")
            elif owner is self.handover:
                if args == (self.journal_storage, JOURNAL, 128 * 1024):
                    self.calls.append("read-handover-journal")
                elif args == (self.journal_storage, KEY, 32):
                    self.calls.append("read-handover-key")
                else:
                    raise AssertionError(
                        "inactive predecessor changed its journal read"
                    )
        elif code is self.handover and owner is self.inactive:
            if event == "call":
                if local["root"] is not self.root:
                    raise AssertionError(
                        "inactive predecessor changed the journal root"
                    )
                self.calls.append("call-handover-journal")
            elif event == "return":
                self.journal = value
                self.calls.append("return-handover-journal")
        elif code is self.require_inactive and owner is self.inactive:
            if event == "call":
                self.calls.append("call-require-inactive")
            elif event == "return":
                self.calls.append("return-require-inactive")
        else:
            return None
        frame.f_trace_lines = False
        return self.trace


def _seed(
    case: str,
    runtime: Path,
    root: Path,
    source: Mapping[str, Any],
) -> dict[str, Any]:
    from core_runtime import development_host_handover_guard as handover
    from core_runtime.profile_definition_store_v4 import ProfileDefinitionStore
    from ecosystem.defaultspack.defaultspack.development_host_data import (
        _sanitize_definition,
    )
    from tobkiri_protocol.canonical import canonical_digest

    if (
        Path(_sanitize_definition.__code__.co_filename).resolve()
        != runtime / SANITIZER_SOURCE
    ):
        raise AssertionError("unexpected import fixture sanitizer")
    store = ProfileDefinitionStore(root, clock=lambda: 1_700_000_000)
    if case == "missing-row":
        store.create_profile(source, profile_id="inactive-registry-unrelated")
    elif case == "different-row-no-handover":
        store.create_profile(source, display_name="Different registry fixture")
    else:
        store.bootstrap_defaults(source)
    if case == "tombstone":
        store.delete_profile(source["profile_id"])
    if case == "authenticated-handover":
        prior = store.snapshot()
        inert = _sanitize_definition(
            dict(source) | {"display_name": "Inert import fixture"}
        )

        def completed(snapshot: Mapping[str, Any]) -> None:
            handover.write_handover_journal(
                root,
                {
                    "stage": "completed",
                    "destination_profile_generation": snapshot["generation"],
                    "destination_profile_snapshot_digest": canonical_digest(snapshot),
                    "synthetic_test_only": True,
                },
            )

        store.import_profile_successors(
            [inert],
            expected_store_generation=prior["generation"],
            expected_snapshot_digest=canonical_digest(prior),
            on_committed=completed,
        )
    return store.snapshot()


def _check(
    case: str,
    observed: _Observation,
    registry: dict[str, Any],
    expected: dict[str, Any],
) -> None:
    from tobkiri_protocol.canonical import canonical_digest

    profile_id = observed.source["profile_id"]
    workspace = f"workspaces/{profile_id}/activation/active.json"
    active_paths = [
        [operation, path, False]
        for path in ("profiles/active.json", workspace)
        for operation in ("exists", "is-symlink")
    ]
    paths = [
        *active_paths,
        ["exists", INDEX, True],
        ["is-symlink", ".", False],
        ["is-symlink", "profiles", False],
    ]
    calls = [
        "call-inactive-predecessor",
        "call-registry-constructor",
        "return-registry-constructor",
        "call-registry-snapshot",
        "acquire-registry-lock",
        "read-registry-index",
        "release-registry-lock",
        "return-registry-snapshot",
    ]
    finish = "none"
    outcome: dict[str, Any] = {"outcome": "returned", "predecessor": None}
    entries = [
        entry for entry in registry["profiles"] if entry["profile_id"] == profile_id
    ]
    if case == "missing-row":
        if entries or len(registry["profiles"]) != 1:
            raise AssertionError(
                "missing row fixture did not contain its unrelated row"
            )
    else:
        [entry] = entries
        existing = entry["revisions"][-1]["profile"]
        if case == "tombstone":
            if not entry["tombstone"] or existing is not None:
                raise AssertionError("tombstone fixture lacks a real tombstone")
            finish = "error"
            outcome = {
                "outcome": "error",
                "error": {
                    "id": 0,
                    "type": (
                        "core_runtime.profile_definition_store_v4."
                        "ProfileDefinitionStoreConflict"
                    ),
                    "message": "imported bootstrap Profile is tombstoned",
                    "suppressed": False,
                    "cause": None,
                    "context": None,
                },
            }
        elif case == "exact-mapping-equal":
            if existing != observed.source or existing is observed.source:
                raise AssertionError(
                    "equal fixture did not load an independent equal mapping"
                )
        else:
            if existing == observed.source or entry["tombstone"]:
                raise AssertionError(
                    "changed fixture did not load a different live mapping"
                )
            authenticated = case == "authenticated-handover"
            calls.append("call-handover-journal")
            paths.extend([["exists", ".", True], ["exists", JOURNAL, authenticated]])
            if not authenticated:
                paths.append(["is-symlink", JOURNAL, False])
                paths.extend(
                    [operation, path, False]
                    for path in (
                        KEY,
                        "development-host-staging",
                        f"packvm-vz/{JOURNAL}",
                    )
                    for operation in ("exists", "is-symlink")
                )
                calls.append("return-handover-journal")
                if observed.journal is not None:
                    raise AssertionError(
                        "missing handover unexpectedly returned a journal"
                    )
            else:
                calls.extend(
                    [
                        "read-handover-journal",
                        "read-handover-key",
                        "return-handover-journal",
                        "call-require-inactive",
                        "return-require-inactive",
                    ]
                )
                paths.extend(active_paths)
                journal = observed.journal
                if (
                    journal["stage"] != "completed"
                    or "authentication" in journal
                    or journal["destination_profile_generation"]
                    != registry["generation"]
                    or journal["destination_profile_snapshot_digest"]
                    != canonical_digest(registry)
                ):
                    raise AssertionError(
                        "authenticated fixture did not read the committed fence"
                    )
                finish = "binding"
                shell = existing["shell"]
                outcome["predecessor"] = {
                    "profile_revision": entry["current_revision"],
                    "definition_digest": canonical_digest(existing),
                    "store_generation": registry["generation"],
                    "snapshot_digest": canonical_digest(registry),
                    "handover_digest": canonical_digest(journal),
                    "display_name": existing["display_name"],
                    "pack_ids": [pack["pack_id"] for pack in existing["packs"]],
                    "base_pack_id": existing["base"]["pack_id"],
                    "shell_pack_id": shell["pack_id"],
                    "shell_provider_id": shell["provider_id"],
                    "shell_artifact_digest": shell["artifact_digest"],
                    "shell_executable_artifact_digest": shell[
                        "executable_artifact_digest"
                    ],
                    "shell_definition_revision": shell["definition_revision"],
                }
    calls.append(f"finish-inactive-predecessor-{finish}")
    if (
        observed.calls != calls
        or observed.path_checks != paths
        or expected != outcome
        or observed.registry != registry
        or observed.registry is registry
    ):
        raise AssertionError((case, observed.calls, observed.path_checks, expected))


def observe_inactive_registry(
    original: Path,
    runtime: Path,
    bundle: Path,
    output: Path,
) -> None:
    """Retain branch observations and keep original fixtures on the CI runner."""
    from core_runtime.profile_definition_store_v4 import ProfileDefinitionStoreConflict
    from tobkiri_protocol.bundle_catalog import BundledCatalog
    from tobkiri_protocol.canonical import canonical_bytes

    def hashes(root: Path) -> dict[str, str]:
        return {
            name: hashlib.sha256((root / name).read_bytes()).hexdigest()
            for name in SOURCES
        }

    original_runtime = original / "tobkiri_runtime"
    source_hashes = hashes(original_runtime)
    if hashes(runtime) != source_hashes:
        raise AssertionError(
            "staged inactive registry sources differ from pinned original"
        )
    catalog = BundledCatalog.load(bundle)
    source = catalog.profiles["defaults"]
    inputs = output / "inactive-registry-inputs"
    inputs.mkdir(mode=0o700)
    (inputs / "source-profile.json").write_bytes(canonical_bytes(source))
    rows = []
    for case in CASES:
        root = inputs / case / "user-data"
        registry = _seed(case, runtime, root, source)
        files = sorted(
            [LOCK, INDEX, *([KEY, JOURNAL] if case == "authenticated-handover" else [])]
        )
        before = _inventory(root, files)
        observed = _Observation(runtime, root, source)
        previous = sys.gettrace()
        try:
            sys.settrace(observed.trace)
            returned = observed.helper(user_data=root, source=source)
        except ProfileDefinitionStoreConflict as error:
            if error is not observed.failure:
                raise AssertionError(
                    "inactive registry lost its original exception"
                ) from error
            expected = {"outcome": "error", "error": error_graph(error, root)}
        else:
            expected = {"outcome": "returned", "predecessor": returned}
        finally:
            sys.settrace(previous)
        _check(case, observed, registry, expected)
        if _inventory(root, files) != before:
            raise AssertionError(
                (case, "original inactive lookup changed fixture files")
            )
        row = {
            "case": case,
            "calls": observed.calls,
            "path_checks": observed.path_checks,
            "files_after": files,
            "files_unchanged": True,
            "expected": expected,
        }
        if case == "authenticated-handover":
            row["setup_review"] = _observe_setup_review(runtime, root, expected["predecessor"])
            if _inventory(root, files) != before:
                raise AssertionError("original Setup review changed the import")
            row["publication_checks"] = _observe_publication_checks(
                runtime, bundle, root, inputs, files,
                row["setup_review"]["confirmation"],
            )
            row["registration_cases"] = _observe_registration(
                runtime, root, inputs, source, expected["predecessor"],
            )
        rows.append(row)
    if hashes(runtime) != source_hashes or hashes(original_runtime) != source_hashes:
        raise AssertionError("inactive registry sources changed during observation")
    write_json(
        output / "inactive-registry.json",
        {
            "format": FORMAT,
            "original_head": ORIGINAL_HEAD,
            "cases": rows,
            "source_hashes": source_hashes,
            "qualification": False,
            "production_enabled": False,
        },
    )


def _observe_setup_review(
    runtime: Path, root: Path, predecessor: dict[str, Any],
) -> dict[str, Any]:
    from core_runtime.api.setup_handlers import SetupHandlersMixin
    from ecosystem.defaultspack.defaultspack.profile_runtime_composition import (
        DefaultspackProfileRuntime,
    )
    from tobkiri_protocol.canonical import canonical_digest

    with patch.dict(os.environ, {
        "TOBKIRI_USER_DATA": str(root),
        "RUMI_USER_DATA": str(root),
        "RUMI_ENVIRONMENT": "production",
        "RUMI_APP_DIR": "",
    }):
        listing = SetupHandlersMixin._setup_listing()
    profile = listing["recommended_default_profile"]
    confirmation = profile["confirmation"]
    digest = confirmation["confirmation_digest"]
    if (
        listing["state"] != "review_required"
        or listing["denial_diagnostic"] is not None
        or confirmation["definition_predecessor"] != predecessor
        or "pack_pin_predecessor" in confirmation
        or digest != canonical_digest({
            key: item for key, item in confirmation.items()
            if key != "confirmation_digest"
        })
    ):
        raise AssertionError("original Setup did not bind the authenticated import")
    decide = DefaultspackProfileRuntime().setup_activation_decision
    if Path(decide.__code__.co_filename).resolve() != runtime / SETUP_RUNTIME_SOURCE:
        raise AssertionError("Setup decision callable is outside the staged original")
    body = {
        "setup_api_version": "io.tobkiri.setup-state.v4",
        "operation_id": "defaults.activate",
        "confirmed": True,
        "confirmation": copy.deepcopy(confirmation),
    }
    body_before = copy.deepcopy(body)
    listing_before = copy.deepcopy(listing)
    preflight = decide(body, None)
    accepted = decide(body, listing)
    stale_listing = copy.deepcopy(listing)
    stale_listing["recommended_default_profile"]["confirmation"]["confirmation_digest"] = (
        "stale-fixture-digest"
    )
    stale_before = copy.deepcopy(stale_listing)
    stale = decide(body, stale_listing)
    integer_body = copy.deepcopy(body)
    integer_body["confirmed"] = 1
    integer_before = copy.deepcopy(integer_body)
    denied = decide(integer_body, None)
    stale_response = {
        "error": "Harness Profile confirmation is stale or tampered",
        "status_code": 409,
        "state": "review_required",
        "write_set": [],
    }
    denied_response = {
        "error": "Harness Profile requires explicit confirmation",
        "status_code": 409,
        "state": "confirmation_required",
        "write_set": [],
    }
    if (
        preflight.response is not None
        or accepted.response is not None
        or preflight.confirmation != confirmation
        or accepted.confirmation != confirmation
        or preflight.confirmation is body["confirmation"]
        or accepted.confirmation is body["confirmation"]
        or accepted.confirmation is preflight.confirmation
        or stale.response != stale_response
        or stale.confirmation is not None
        or denied.response != denied_response
        or denied.confirmation is not None
        or body != body_before
        or listing != listing_before
        or stale_listing != stale_before
        or integer_body != integer_before
    ):
        raise AssertionError("original Setup activation decisions differ")
    return {
        "decision_observation": {
            "preflight": "confirmation-copy",
            "listing_match": "confirmation-copy",
            "stale_listing": stale_response,
            "integer_confirmed_preflight": denied_response,
            "inputs_unchanged": True,
        },
        "state": listing["state"],
        "profile_id": profile["profile_id"],
        "confirmation": confirmation,
        "listing_digest": canonical_digest(listing),
    }


class _PublicationBoundary(BaseException):
    """Stop before the original journal creates a key or intent."""


def _observe_publication_checks(
    runtime: Path,
    bundle: Path,
    seed: Path,
    inputs: Path,
    files: list[str],
    confirmation: dict[str, Any],
) -> list[dict[str, Any]]:
    """Observe confirmed capture under the real lock up to the first intent."""
    from core_runtime.active_profile_store_v4 import ActiveProfilePointer
    from core_runtime.bootstrap import profile_capture as capture
    from core_runtime.bootstrap.profile_publication import ProfilePublicationJournal
    from core_runtime.profile_runtime_port import require_profile_runtime
    from tobkiri_protocol.canonical import canonical_digest

    port = require_profile_runtime()
    port_type = type(port)
    load = port_type.load_catalog
    capture_code = capture._capture_bootstrap_profile.__code__
    lock = bundle / "bundle.lock.json"
    lock_before = lock.read_bytes()
    publication_lock = "profiles/.bootstrap-publication.lock"
    for method, relative in (
        (capture.capture_bootstrap_profile, CAPTURE_SOURCE),
        (capture._capture_bootstrap_profile, CAPTURE_SOURCE),
        (capture.host_profile_catalog, CAPTURE_SOURCE),
        (ProfilePublicationJournal.begin, PUBLICATION_SOURCE),
        (load, SETUP_RUNTIME_SOURCE),
    ):
        if Path(method.__code__.co_filename).resolve() != runtime / relative:
            raise AssertionError("confirmed capture callable is outside the staged original")

    rows = []
    for case in (
        "matched", "stale-confirmation", "changed-source", "changed-lock", "stale-intent",
    ):
        root = inputs / f"publication-{case}" / "user-data"
        shutil.copytree(seed, root)
        kept = list(files)
        if case == "stale-intent":
            # Use the actual writer to authenticate a prior intent expecting a
            # Host pointer that is now absent. Confirmed recovery must keep it.
            previous = ActiveProfilePointer(
                profile_id="defaults", profile_revision="sha256:" + "1" * 64,
                activation_id="activation:previous", plan_digest="sha256:" + "2" * 64,
                lock_digest="sha256:" + "3" * 64,
                activation_snapshot_path="workspaces/defaults/activation/activations/previous.json",
                activation_snapshot_digest="sha256:" + "4" * 64,
                catalog_revision=None, generation=1, updated_at=REGISTRATION_TIME,
            )
            resolved = SimpleNamespace(
                profile={"profile_id": "defaults"},
                plan={
                    "profile_revision": confirmation["profile_revision"],
                    "plan_digest": confirmation["plan_digest"],
                    "profile_definition_digest": "sha256:" + "5" * 64,
                    "security_epoch": confirmation["security_epoch"],
                },
                lock={"lock_digest": "sha256:" + "6" * 64},
            )
            ProfilePublicationJournal(root).begin(
                predecessor=previous, resolved=resolved, activation_id="activation:fixture",
                confirmation=confirmation,
            )
            kept.extend(("profiles/.publication-key", "profiles/bootstrap-publication.json"))
        existing = _inventory(root, kept)
        approved = copy.deepcopy(confirmation)
        if case == "stale-confirmation":
            approved["confirmation_digest"] = "stale-fixture-digest"
        before = copy.deepcopy(approved)
        host_catalog_seen = False
        reread = None
        candidate = None
        events = []

        def load_catalog(self: Any, location: Path) -> Any:
            nonlocal reread
            # Only the last independent read is changed. The re-resolver and
            # Host catalog loader still use their actual inputs and behavior.
            caller = sys._getframe(1).f_code
            result = load(self, location)
            if caller is not capture_code:
                return result
            if location != bundle or not host_catalog_seen:
                raise AssertionError("original capture skipped Host catalog materialization")
            if _lock_free(root / publication_lock) is not False or reread is not None:
                raise AssertionError("original capture did not hold its unique publication lock")
            events.append("reread-catalog-under-publication-lock")
            profile_id = confirmation["profile_id"]
            if case == "changed-source":
                source = dict(result.profiles[profile_id], display_name="Changed after review")
                result = replace(result, profiles={**result.profiles, profile_id: source})
            elif case == "changed-lock":
                lock.write_bytes(lock_before + b"\n")
            reread = result
            return result

        def watch_host_catalog(*args: Any, **kwargs: Any) -> Any:
            nonlocal host_catalog_seen
            if _lock_free(root / publication_lock) is not False or host_catalog_seen:
                raise AssertionError("original Host catalog ran outside the publication lock")
            events.append("materialize-host-catalog")
            result = host_catalog(*args, **kwargs)
            host_catalog_seen = True
            return result

        def stop_begin(
            journal: Any, *, predecessor: Any, resolved: Any,
            activation_id: str, confirmation: Mapping[str, Any],
        ) -> None:
            nonlocal candidate
            if (
                case != "matched"
                or journal.root != root / "profiles"
                or _lock_free(root / publication_lock) is not False
                or predecessor is not None
                or confirmation is not approved
                or reread is None
            ):
                raise AssertionError("original publication admitted an unexpected candidate")
            profile_id = resolved.profile["profile_id"]
            plan_digest = resolved.plan["plan_digest"]
            definition_digest = resolved.plan["profile_definition_digest"]
            bundle_digest = resolved.lock["bundle_digest"]
            if (
                profile_id != approved["profile_id"]
                or plan_digest != approved["plan_digest"]
                or definition_digest != canonical_digest(reread.profiles[profile_id])
                or bundle_digest != "sha256:" + hashlib.sha256(lock_before).hexdigest()
                or activation_id != f"activation:{profile_id}-{plan_digest[7:23]}"
            ):
                raise AssertionError("original publication candidate identities differ")
            candidate = {
                "profile_id": profile_id,
                "profile_definition_digest": definition_digest,
                "bundle_digest": bundle_digest,
                "plan_digest": plan_digest,
                "activation_id": activation_id,
            }
            events.append("before-first-publication-intent")
            raise _PublicationBoundary

        host_catalog = capture.host_profile_catalog
        try:
            with (
                patch.dict(os.environ, {
                    "TOBKIRI_USER_DATA": str(root), "RUMI_USER_DATA": str(root),
                    "RUMI_ENVIRONMENT": "production", "RUMI_APP_DIR": "",
                }),
                patch.object(port_type, "load_catalog", load_catalog),
                patch.object(capture, "host_profile_catalog", watch_host_catalog),
                patch.object(ProfilePublicationJournal, "begin", stop_begin),
            ):
                capture.capture_bootstrap_profile(confirmation=approved)
        except _PublicationBoundary:
            outcome = {"candidate": candidate}
        except Exception as error:
            expected = {
                "stale-confirmation": "bootstrap activation confirmation is stale or tampered",
                "stale-intent": "Profile publication predecessor is stale",
            }.get(case, "bootstrap target changed after confirmation")
            if case == "matched" or not port.is_resolution_denied(error) or str(error) != expected:
                raise
            outcome = {"error": error_graph(error, root)}
        else:
            raise AssertionError("capture passed the first publication intent")
        finally:
            if case == "changed-lock":
                lock.write_bytes(lock_before)
        after = _inventory(root, [*kept, publication_lock])
        same_entries = all(after[name] == existing[name] for name in kept)
        expected_events = [] if case in {"stale-confirmation", "stale-intent"} else [
            "materialize-host-catalog", "reread-catalog-under-publication-lock",
        ]
        if case == "matched":
            expected_events.append("before-first-publication-intent")
        if (
            events != expected_events or approved != before or not same_entries
            or _lock_free(root / publication_lock) is not True
            or lock.read_bytes() != lock_before
        ):
            raise AssertionError((case, "capture altered its inputs or publication sequence"))
        rows.append({"case": case, "events": events, "outcome": outcome})
    return rows


def _observe_registration(
    runtime: Path,
    seed: Path,
    inputs: Path,
    source: Mapping[str, Any],
    predecessor: dict[str, Any],
) -> list[dict[str, Any]]:
    """Observe the registration function inside the original Host selection lock."""
    from core_runtime.bootstrap.profile_registry import register_bootstrap_definition
    from core_runtime.profile_definition_store_v4 import (
        ProfileDefinitionStore,
        ProfileDefinitionStoreConflict,
    )
    from tobkiri_protocol.canonical import canonical_digest

    original = runtime / "core_runtime/bootstrap/profile_registry.py"
    if Path(register_bootstrap_definition.__code__.co_filename).resolve() != original:
        raise AssertionError("registration callable is outside the staged original")
    cases = []
    for case in ("append", "registry-edited", "host-active", "workspace-active"):
        root = inputs / f"register-{case}" / "user-data"
        shutil.copytree(seed, root)
        store = ProfileDefinitionStore(root)
        profile_id = source["profile_id"]
        active = None
        if case == "registry-edited":
            store.update_profile(profile_id, display_name="Concurrent edit")
        elif case in {"host-active", "workspace-active"}:
            active = root / (
                "profiles/active.json" if case == "host-active"
                else f"workspaces/{profile_id}/activation/active.json"
            )
            active.parent.mkdir(parents=True, exist_ok=True)
            active.symlink_to("missing-fixture-pointer")
        journal_before = (root / JOURNAL).read_bytes()
        before = store.snapshot()
        # Keep the exact input bytes in the runner fixture, including the
        # concurrent edit. The real registration below uses a separate copy.
        initial = inputs / f"register-input-{case}" / "user-data"
        shutil.copytree(root, initial, symlinks=True)
        try:
            with patch.object(ProfileDefinitionStore, "_now", return_value=REGISTRATION_TIME):
                register_bootstrap_definition(
                    root,
                    source,
                    approved_predecessor_digest=predecessor["definition_digest"],
                    approved_inactive_predecessor=predecessor,
                )
        except ProfileDefinitionStoreConflict as error:
            message = (
                "imported bootstrap registry changed after completed handover"
                if case == "registry-edited"
                else "inactive bootstrap predecessor has an active Profile"
            )
            if case == "append" or str(error) != message or store.snapshot() != before:
                raise AssertionError("original registration conflict differs") from error
            expected = {"outcome": "error", "error": error_graph(error, root)}
        else:
            after = store.snapshot()
            existing = store.get_profile(profile_id)
            [previous_entry] = before["profiles"]
            [current_entry] = after["profiles"]
            history = current_entry["revisions"]
            if (
                case != "append"
                or after["generation"] != before["generation"] + 1
                or existing is None
                or existing.profile != source
                or existing.parent_revision != predecessor["profile_revision"]
                or existing.profile_revision != canonical_digest(source)
                or history[:-1] != previous_entry["revisions"]
                or len(history) != len(previous_entry["revisions"]) + 1
            ):
                raise AssertionError("original registration did not append one successor")
            expected = {
                "outcome": "appended",
                "generation": after["generation"],
                "parent_revision": existing.parent_revision,
                "profile_revision": existing.profile_revision,
                "history_length": len(history),
                "snapshot_digest": canonical_digest(after),
            }
        if (
            _lock_free(root / "profiles/.active-profile.lock") is not True
            or _lock_free(root / LOCK) is not True
            or (root / JOURNAL).read_bytes() != journal_before
            or (active is not None and not active.is_symlink())
        ):
            raise AssertionError("original registration changed lock or handover state")
        cases.append({
            "case": case,
            "registration_time": REGISTRATION_TIME,
            "previous_snapshot_digest": canonical_digest(before),
            "expected": expected,
        })
    return cases
