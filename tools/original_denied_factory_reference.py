"""Observe the actual original denial factory; run only in fresh CI storage.

This finite reference observes returned exceptions before any Host raise. It
does not qualify Host canonical validation, recovery or the Launcher GUI.
Scope/release/rollback and native Host cause identity need separate native
tests; observations here are expected DATA, never factory implementations.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys

from original_authority_reference import (
    ORIGINAL_HEAD, first_difference, load_json, write_json,
)
from original_setup_review_reference import check_original


FORMAT = "io.tobkiri.original-denied-factory-fixtures.v2"
CASES = (
    ("empty", ""),
    ("publication-identity", "Profile publication identity changed"),
    ("bootstrap-canonical", "application bootstrap Profile identity is not canonical"),
    ("japanese", "既存のProfileを確認してください"),
    ("nonbmp", "Harness \U0001f9ed"),
    ("quotes-controls", '"\\,:\n\r\t\0'),
    ("whitespace", "\u2003  \u2003"),
    ("same-message-again", "Profile publication identity changed"),
)
SOURCES = (
    "core_runtime/profile_runtime_port.py",
    "ecosystem/defaultspack/defaultspack/profile_runtime_composition.py",
    "ecosystem/defaultspack/domain/runtime_v4/service.py",
)


def returned_exception(error: BaseException, kind: str) -> tuple[dict, str]:
    """Observe the fixed factory's actual builtin exception storage only."""
    args = BaseException.args.__get__(error)
    cause = BaseException.__cause__.__get__(error)
    context = BaseException.__context__.__get__(error)
    suppressed = BaseException.__suppress_context__.__get__(error)
    if (type(args) is not tuple or len(args) != 1 or type(args[0]) is not str
            or cause is not None or context is not None or suppressed is not False):
        raise AssertionError("original fresh-factory contract differs")
    cls = type(error)
    return ({"outcome": "factory-returned", "kind": kind,
             "factory_denied": {"args": list(args), "cause": None,
                                "context": None, "suppress_context": False}},
            cls.__module__ + "." + cls.__qualname__)


def prepare(original: Path, destination: Path) -> None:
    """Call the real installed original port, without replacing its factory."""
    check_original(original)
    runtime = original / "tobkiri_runtime"
    sys.path.insert(0, str(runtime))
    from core_runtime.profile_runtime_port import require_profile_runtime
    from ecosystem.defaultspack.defaultspack.profile_runtime_composition import install_defaultspack_profile_runtime

    install_defaultspack_profile_runtime()
    port = require_profile_runtime()
    results, retained = [], []
    for name, message in CASES:
        error = port.denied(message)
        if not isinstance(error, BaseException) or any(error is prior for prior in retained):
            raise AssertionError("real factory must return a fresh exception")
        retained.append(error)
        # Use the actual application's original classifier, matching the
        # existing adapter kind. Never populate a native observation's class
        # by copying an expected class name from this reference.
        if port.is_resolution_denied(error) is not True:
            raise AssertionError("original port did not classify its returned denial")
        observation, original_type = returned_exception(error, "profile-resolution-denied")
        if observation["factory_denied"]["args"] != [message]:
            raise AssertionError("original factory changed its supplied message")
        results.append({"name": name, "message": message, "expected": observation,
                        "original_type": original_type})
    if len({row["original_type"] for row in results}) != 1:
        raise AssertionError("fixed original factory changed its actual type")
    destination.mkdir(parents=True, mode=0o700, exist_ok=False)
    metadata = {"format": FORMAT, "original_head": ORIGINAL_HEAD, "python": "3.13.16",
                "sources": {name: hashlib.sha256((runtime / name).read_bytes()).hexdigest()
                            for name in SOURCES},
                "cases": results, "reference_checks_passed": True,
                "native_comparison": False, "qualification": False, "production_enabled": False,
                "scope": "actual original factory returns only; no Host raise, canonical validator, "
                         "recovery, product adoption or GUI qualification"}
    write_json(destination / "fixtures.json", metadata)
    summary = {key: value for key, value in metadata.items() if key != "cases"}
    summary["cases"] = [{"name": row["name"], "observation": row["expected"],
                         "original_type": row["original_type"]}
                        for row in results]
    write_json(destination / "summary.json", summary)
    print(json.dumps({"original_factory_cases": len(CASES), "native_comparison": False,
                      "qualification": False, "production_enabled": False}))


def compare(fixtures: Path, observations: Path, output: Path) -> None:
    """Compare DATA; lifecycle/provenance tests remain independently required."""
    expected, native = load_json(fixtures), load_json(observations)
    if (expected["format"] != FORMAT or expected["original_head"] != ORIGINAL_HEAD
            or expected["python"] != "3.13.16" or expected["reference_checks_passed"] is not True
            or native["format"] != "io.tobkiri.original-denied-factory-native-observations.v2"):
        raise ValueError("fresh original/native factory observations required")
    names = {name for name, _ in CASES}
    by_name = {row["name"]: row for row in native["cases"]}
    original = {row["name"]: row for row in expected["cases"]}
    if (set(by_name) != names or set(original) != names
            or len(native["cases"]) != len(CASES) or len(expected["cases"]) != len(CASES)):
        raise ValueError("exact finite factory case inventory required")
    report = {"format": "io.tobkiri.original-denied-factory-comparison.v2",
              "original_head": ORIGINAL_HEAD, "cases": [], "matched": False,
              "native_comparison": True, "qualification": False, "production_enabled": False,
              "scope": expected["scope"]}
    for name, _ in CASES:
        observed = {key: value for key, value in by_name[name].items() if key != "name"}
        report["cases"].append({"name": name,
                                "difference": first_difference(original[name]["expected"], observed)})
    report["matched"] = all(row["difference"] is None for row in report["cases"])
    write_json(output, report)
    if not report["matched"]:
        raise AssertionError([row for row in report["cases"] if row["difference"] is not None])
    print(json.dumps({"native_comparison": True, "matched": True, "cases": len(CASES),
                      "qualification": False, "production_enabled": False}))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prepare", "compare"))
    parser.add_argument("--original-root", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--fixtures", type=Path)
    parser.add_argument("--observations", type=Path)
    args = parser.parse_args()
    if args.command == "prepare":
        if args.original_root is None:
            parser.error("prepare requires --original-root")
        prepare(args.original_root.resolve(strict=True), args.output.resolve())
    else:
        if args.fixtures is None or args.observations is None:
            parser.error("compare requires --fixtures and --observations")
        compare(args.fixtures.resolve(strict=True), args.observations.resolve(strict=True),
                args.output.resolve())


if __name__ == "__main__":
    main()
