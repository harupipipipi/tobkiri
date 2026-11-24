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

from original_authority_reference import ORIGINAL_HEAD, error_graph, first_difference, load_json, write_json
from original_bootstrap_identity_reference import (
    controlled_getter, exception_graph as raw_exception_graph,
    return_cases, raised_cases, returned_value,
)
from original_publication_reference import (
    CASES as PREVIOUS_CASES, SOURCES as PREVIOUS_SOURCES, filesystem, seed,
    ACTIVE_PROFILE_CASES, original_activation_writer, seed_current,
)
from original_setup_review_reference import check_original


FORMAT = "io.tobkiri.original-publication-identity-reference.v1"
GETTER_INPUT_FORMAT = "io.tobkiri.publication-identity-getter-fixture.v1"
SOURCES = (*PREVIOUS_SOURCES, "core_runtime/profile_runtime_port.py",
           "ecosystem/defaultspack/defaultspack/profile_runtime_composition.py")


class RecoveryCatalogBoundary(BaseException):
    """A test stop at a natural call boundary, never an original typed error."""


def getter_fixture(controlled):
    """Keep controlled inputs separate from the observed Host outcomes.

    Non-string contents are outside the original Host type-check reachpoint.
    A native controlled-component fixture needs only that exact top-level kind;
    this does not qualify equality of the original object graph. Strings keep
    all input codepoints here, before the bridge's 129 length sentinel. Raised
    objects keep their original intrinsic edges before recovery can add a Host
    wrapper. These are CI inputs, never a runtime configuration or authority.
    """
    if controlled is None:
        return {"format": GETTER_INPUT_FORMAT, "mode": "unmodified-default"}
    value, raised = controlled
    if raised:
        error = raw_exception_graph(value, lambda e: type(e).__module__ + "." + type(e).__qualname__)
        return {"format": GETTER_INPUT_FORMAT, "mode": "controlled-raised",
                "intrinsic_error": error}
    observed = returned_value(value)
    if type(value) is str:
        observed["codepoints"] = [ord(c) for c in value]
    return {"format": GETTER_INPUT_FORMAT, "mode": "controlled-returned",
            "value": observed, "non_string_top_level_only": type(value) is not str}


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
    default_identity = capture._bootstrap_profile_id()
    if default_identity != "defaults":
        raise AssertionError("unmodified default getter changed")
    denial_probe = port.denied("Profile publication identity changed")
    if port.is_resolution_denied(denial_probe) is not True:
        raise AssertionError("actual original factory classifier changed")
    if BaseException.args.__get__(denial_probe) != ("Profile publication identity changed",):
        raise AssertionError("actual original denial message arguments changed")
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
        raw_input = getter_fixture(controlled)
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
                        "getter_fixture": raw_input,
                        "controlled_raw_getter": controlled is not None,
                        "after_lock_remove_intent": name == "intent-disappears-after-lock",
                        "between_calls_install_intent": name == "current-stale-installed-between-recovery-calls",
                        "expected": outcome, "reference_state": state})
    by_name = {row["name"]: row for row in reports}
    input_modes = {mode: sum(row["getter_fixture"]["mode"] == mode for row in reports)
                   for mode in ("unmodified-default", "controlled-returned", "controlled-raised")}
    if input_modes != {"unmodified-default": 78, "controlled-returned": 58, "controlled-raised": 11}:
        raise AssertionError("closed native controlled-input inventory changed")
    for row in reports:
        item = row["getter_fixture"]
        if item["format"] != GETTER_INPUT_FORMAT:
            raise AssertionError("input fixture format differs")
        if item["mode"] == "controlled-returned" and item["value"]["kind"] == "str":
            value = item["value"]
            if len(value["codepoints"]) != value["length"]:
                raise AssertionError("input string was clipped before native bridge observation")
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
                "unmodified_default_identity": default_identity,
                "application_denial_class": type(denial_probe).__module__ + "." + type(denial_probe).__qualname__,
                "application_denial_kind": "profile-resolution-denied",
                "sources": {name: hashlib.sha256((runtime / name).read_bytes()).hexdigest()
                            for name in SOURCES}, "cases": reports,
                "original_previous_inputs_retained": 77, "controlled_raw_getter_cases": 69,
                "candidate_mismatch_cases": 1, "reference_checks_passed": True,
                "getter_fixture_format": GETTER_INPUT_FORMAT,
                "getter_fixture_modes": input_modes,
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


def compare_actual_default(original: Path, fixtures: Path, observations: Path, output: Path) -> None:
    """Compare two real-component operations; the controlled147 remain separate."""
    check_original(original)
    reference, native = load_json(fixtures), load_json(observations)
    if (reference["format"] != FORMAT or reference["original_head"] != ORIGINAL_HEAD
            or reference["reference_checks_passed"] is not True
            or native["format"] != "io.tobkiri.actual-default-publication-identity-observations.v1"):
        raise ValueError("fresh original/default-component observations required")
    names = {"valid", "identity-candidate-mismatch"}
    actual = {row["name"]: row for row in native["cases"]}
    source = {row["name"]: row for row in reference["cases"]}
    if set(actual) != names or len(native["cases"]) != 2 or len(source) != 147:
        raise ValueError("exact two actual default cases required")
    report = {"format": "io.tobkiri.actual-default-publication-identity-comparison.v1",
              "original_head": ORIGINAL_HEAD, "cases": [], "matched": False,
              "native_comparison": True, "qualification": False, "production_enabled": False,
              "scope": "two real-component default-getter operations only; no controlled147, "
                       "full recovery, close-fault, actual Tauri worker Join or GUI qualification"}
    for name in sorted(names):
        expected, observed = source[name], actual[name]
        if expected["getter_fixture"]["mode"] != "unmodified-default":
            raise ValueError("controlled input cannot qualify actual default path")
        row = observed["observation"]
        if not isinstance(row, dict):
            raise ValueError("actual operation observation missing")
        for flag in ("execution_matched_branch", "cleanup_verified",
                     "journal_lock_released_after_finish", "source_destructor_reached"):
            if observed.get(flag) is not True:
                raise AssertionError((name, flag))
        if (row["journal_lock_held_before_finish"] is not True
                or row["uncertain_before_finish"] is not True
                or row["same_original_deadline"] is not True
                or row["budget_resets"] != 0 or row["extra_inspections"] != 0
                or not 0 <= row["fuel_after"] < row["fuel_before"] <= 2_000_000_000
                or observed["capacity"]["after_reap"] != {"bytes": 0, "reservations": 0}):
            raise AssertionError((name, "actual scope/budget/cleanup proof differs"))
        if row["getter"]["string"] != {
                "length": len(reference["unmodified_default_identity"]),
                "codepoints": [ord(c) for c in reference["unmodified_default_identity"]]}:
            raise AssertionError((name, "actual original getter differs"))
        outcome = row["outcome"]
        if name == "valid":
            difference = first_difference(expected["expected"], outcome)
        else:
            original_error = expected["expected"]["error"]
            if (expected["expected"]["outcome"] != "error"
                    or original_error["type"] != reference["application_denial_class"]
                    or original_error["cause"] is not None or original_error["context"] is not None
                    or original_error["suppressed"] is not False):
                raise AssertionError("original mismatch raise is not a plain classified denial")
            wanted = {"outcome": "actual-denied", "kind": reference["application_denial_kind"],
                      "factory_denied": {"args": [original_error["message"]], "cause": None,
                                         "context": None, "suppress_context": False},
                      "host_cause": None, "host_context": None, "host_suppress_context": False}
            difference = first_difference(wanted, outcome)
        root = Path(expected["user_data_dir"])
        if root != fixtures.parent / name / "native":
            raise ValueError("actual fixture root was substituted")
        state_difference = first_difference(expected["reference_state"], filesystem(root))
        if ((root / "profiles/.publication-key").read_bytes()
                != (root.parent / "seed/profiles/.publication-key").read_bytes()):
            raise AssertionError("existing original publication key changed")
        report["cases"].append({"name": name, "outcome_difference": difference,
                                "filesystem_difference": state_difference,
                                "existing_key_unchanged": True})
    report["matched"] = all(row["outcome_difference"] is None and row["filesystem_difference"] is None
                            for row in report["cases"])
    write_json(output, report)
    if not report["matched"]:
        raise AssertionError("actual default recovery comparison differs")
    print({"actual_default_cases": 2, "matched": True, "qualification": False})


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", nargs="?", choices=("prepare", "compare-actual-default"), default="prepare")
    parser.add_argument("--original-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--fixtures", type=Path)
    parser.add_argument("--observations", type=Path)
    args = parser.parse_args()
    root = args.original_root.resolve(strict=True)
    if args.command == "prepare":
        prepare(root, args.output.resolve())
    else:
        if args.fixtures is None or args.observations is None:
            parser.error("compare-actual-default requires --fixtures and --observations")
        compare_actual_default(root, args.fixtures.resolve(strict=True),
                               args.observations.resolve(strict=True), args.output.resolve())


if __name__ == "__main__":
    main()
