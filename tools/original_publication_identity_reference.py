"""Observe real original recovery through identity comparison to catalog entry.

The old 77-case oracle remains unchanged. This larger finite boundary executes
the original helper/validator/factory, workspace Path construction and immutable
runtime lookup. Only host_profile_catalog is replaced by an explicit test stop;
its body, Authority construction and workspace.exists are not executed.
"""
from __future__ import annotations

import argparse
from contextlib import nullcontext
import hashlib
import os
from pathlib import Path
import shutil
import sys

from original_authority_reference import ORIGINAL_HEAD, error_graph, write_json
from original_bootstrap_identity_reference import controlled_getter, return_cases, raised_cases
from original_publication_reference import (
    CASES as PREVIOUS_CASES, SOURCES as PREVIOUS_SOURCES, filesystem, seed,
    ACTIVE_PROFILE_CASES, original_activation_writer, seed_current,
)
from original_setup_review_reference import check_original


FORMAT = "io.tobkiri.original-publication-identity-reference.v1"
SOURCES = (*PREVIOUS_SOURCES, "core_runtime/profile_runtime_port.py",
           "ecosystem/defaultspack/defaultspack/profile_runtime_composition.py")


class RecoveryCatalogBoundary(BaseException):
    """A test stop at a natural call boundary, never an original typed error."""


def observe(root, name, capture, journal_type, port, controlled=None):
    """Keep actual recovery/finally and the original helper/factory unchanged."""
    original_catalog, original_read = capture.host_profile_catalog, journal_type.read
    original_identity = capture._bootstrap_profile_id
    original_getter = type(port).__dict__["bootstrap_profile_id"]
    previous = {key: os.environ.get(key) for key in ("TOBKIRI_USER_DATA", "RUMI_USER_DATA")}
    boundary_calls = []

    def stop_catalog(base_dir=None):
        boundary_calls.append("host_profile_catalog-entry")
        raise RecoveryCatalogBoundary

    def disappearing_read(journal):
        journal.path.unlink()
        return original_read(journal)

    binding = (controlled_getter(port, controlled[0], controlled[1])
               if controlled is not None else nullcontext(None))
    try:
        capture.host_profile_catalog = stop_catalog
        if name == "intent-disappears-after-lock":
            journal_type.read = disappearing_read
        for key in previous:
            os.environ[key] = str(root)
        with binding as getter_calls:
            try:
                for discard in (True, False):
                    capture._recover_bootstrap_publication(discard_superseded=discard)
                    if discard and name == "current-stale-installed-between-recovery-calls":
                        profiles = root / "profiles"
                        (profiles / ".fixture-pending-publication.json").rename(
                            profiles / "bootstrap-publication.json")
                result = {"outcome": "integration-boundary", "stage": "catalog-not-bound"}
            except RecoveryCatalogBoundary:
                result = {"outcome": "integration-boundary", "stage": "recovery-catalog-not-bound"}
            except BaseException as error:
                result = {"outcome": "error", "error": error_graph(error, root)}
            if getter_calls is not None and getter_calls != ["raw-getter"]:
                raise AssertionError("controlled getter was not called exactly once")
        if capture._bootstrap_profile_id is not original_identity:
            raise AssertionError("original canonical helper was replaced")
        if len(boundary_calls) > 1:
            raise AssertionError("catalog boundary repeated")
        return result
    finally:
        capture.host_profile_catalog, journal_type.read = original_catalog, original_read
        if type(port).__dict__["bootstrap_profile_id"] is not original_getter:
            raise AssertionError("controlled getter restoration failed")
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def prepare(original: Path, destination: Path) -> None:
    """Seed independently, then execute actual original state changes and errors."""
    check_original(original)
    runtime = original / "tobkiri_runtime"
    sys.path.insert(0, str(runtime))
    from core_runtime.bootstrap import profile_capture as capture
    from core_runtime.bootstrap.profile_publication import ProfilePublicationJournal
    from core_runtime.active_profile_store_v4 import ActiveProfilePointer, ActiveProfileStore
    from core_runtime.profile_runtime_port import require_profile_runtime
    from core_runtime import hmac_key_manager as signing
    from ecosystem.defaultspack.defaultspack.profile_runtime_composition import install_defaultspack_profile_runtime
    from tobkiri_protocol import canonical as protocol

    install_defaultspack_profile_runtime()
    port = require_profile_runtime()
    if capture._bootstrap_profile_id() != "defaults":
        raise AssertionError("unmodified default getter changed")
    write_activation = original_activation_writer(runtime, protocol)
    destination.mkdir(mode=0o700, parents=True, exist_ok=False)
    inventory = [(name, name, None) for name in PREVIOUS_CASES]
    inventory += [("identity-returned-" + name, "valid", (value, False))
                  for name, value in return_cases()]
    inventory += [("identity-raised-" + name, "valid", (error, True))
                  for name, error in raised_cases()]
    inventory.append(("identity-candidate-mismatch", "valid", None))
    if len(inventory) != 147 or len({name for name, _, _ in inventory}) != 147:
        raise AssertionError("closed larger-boundary case inventory changed")
    reports = []
    for name, seed_name, controlled in inventory:
        directory = destination / name
        directory.mkdir(mode=0o700)
        seed_root = directory / "seed"
        seed(seed_name, seed_root, ProfilePublicationJournal, ActiveProfilePointer, protocol, signing)
        if name in ACTIVE_PROFILE_CASES:
            seed_current(name, seed_root, ActiveProfileStore, write_activation, protocol, signing)
        if name == "identity-candidate-mismatch":
            journal = ProfilePublicationJournal(seed_root)
            envelope = protocol.strict_loads(journal.path.read_bytes())
            envelope["payload"]["candidate"]["profile_id"] = "another"
            key = signing.load_signing_key(seed_root / "profiles" / ".publication-key")
            envelope["hmac"] = signing.compute_data_hmac(key, envelope["payload"])
            journal.path.write_bytes(protocol.canonical_json(envelope) + b"\n")
        for target in ("reference", "native"):
            if seed_root.exists():
                shutil.copytree(seed_root, directory / target, symlinks=True)
        reference, native = directory / "reference", directory / "native"
        outcome = observe(reference, name, capture, ProfilePublicationJournal, port, controlled)
        state = filesystem(reference)
        if any(row["path"].split("/")[0] == "authority" for row in state["files"]):
            raise AssertionError("reference crossed the Authority constructor")
        reports.append({"name": name, "user_data_dir": str(native),
                        "controlled_raw_getter": controlled is not None,
                        "after_lock_remove_intent": name == "intent-disappears-after-lock",
                        "between_calls_install_intent": name == "current-stale-installed-between-recovery-calls",
                        "expected": outcome, "reference_state": state})
    by_name = {row["name"]: row for row in reports}
    for name in ("valid", "profiles-broad-mode", "key-unicode-valid", "key-unicode-whitespace",
                 "current-absent", "current-same-parent", "current-immediately-published",
                 "snapshot-wrapper-envelope", "identity-returned-defaults"):
        if by_name[name]["expected"] != {"outcome": "integration-boundary", "stage": "recovery-catalog-not-bound"}:
            raise AssertionError((name, "actual canonical identity/catalog entry not reached"))
    mismatch = by_name["identity-candidate-mismatch"]["expected"]
    if (mismatch["outcome"] != "error"
            or mismatch["error"]["message"] != "Profile publication identity changed"
            or mismatch["error"]["cause"] is not None or mismatch["error"]["context"] is not None):
        raise AssertionError("original identity mismatch has different raise edges")
    if require_profile_runtime() is not port or capture._bootstrap_profile_id() != "defaults":
        raise AssertionError("original immutable runtime changed")
    metadata = {"format": FORMAT, "original_head": ORIGINAL_HEAD, "python": "3.13.16",
                "sources": {name: hashlib.sha256((runtime / name).read_bytes()).hexdigest()
                            for name in SOURCES}, "cases": reports,
                "original_previous_inputs_retained": 77, "controlled_raw_getter_cases": 69,
                "candidate_mismatch_cases": 1, "reference_checks_passed": True,
                "native_comparison": False, "qualification": False, "production_enabled": False,
                "catalog_body_executed": False, "authority_constructor_executed": False,
                "scope": "same original publication guard through getter/canonical/identity "
                         "comparison/workspace Path/runtime lookup to catalog entry; test stop "
                         "is not a typed original error; actual default and controlled getters "
                         "are separate; no catalog/Authority/workspace.exists or full review"}
    write_json(destination / "fixtures.json", metadata)
    summary = {key: value for key, value in metadata.items() if key != "cases"}
    summary["cases"] = [{"name": row["name"], "controlled_raw_getter": row["controlled_raw_getter"],
                         "observation": row["expected"], "reference_state": row["reference_state"]}
                        for row in reports]
    write_json(destination / "summary.json", summary)
    print({"original_publication_identity_cases": 147, "native_comparison": False,
           "qualification": False, "production_enabled": False})


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--original-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    prepare(args.original_root.resolve(strict=True), args.output.resolve())


if __name__ == "__main__":
    main()
