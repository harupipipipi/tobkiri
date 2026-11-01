"""CI-only reference for native-origin recovery catch and Host raise semantics.

Construct the actual original exception classes as controlled catalog failures.
The original recovery body, journal guard and installed denied factory execute
unchanged. This does not prove native IO origins, Guest cause materialization,
migrations or GUI adoption. Expected observations never enter a native factory.
"""
from __future__ import annotations

import argparse
import errno
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from types import MethodType

from original_authority_reference import ORIGINAL_HEAD, first_difference, write_json
from original_catalog_prefix_reference import code_hashes
from original_profile_definition_prefix_reference import _lock_free
from original_publication_reference import filesystem
from original_setup_review_reference import check_original

FORMAT = "io.tobkiri.original-recovery-error-reference.v1"
CASES = (
    "secure-message", "active-base", "active-integrity", "active-lock-timeout",
    "timeout-message", "timeout-errno", "secure-chained", "active-chained",
    "registry-integrity", "registry-conflict",
)
EXCLUDED_NATIVE_CASES = ("timeout-message", "timeout-errno")
NATIVE_CASES = tuple(name for name in CASES if name not in EXCLUDED_NATIVE_CASES)
SOURCE_PATHS = (
    "core_runtime/bootstrap/profile_capture.py",
    "core_runtime/active_profile_store_v4.py",
    "core_runtime/profile_definition_store_v4.py",
    "tobkiri_protocol/secure_persistence.py",
    "core_runtime/profile_runtime_port.py",
    "ecosystem/defaultspack/defaultspack/profile_runtime_composition.py",
    "ecosystem/defaultspack/domain/runtime_v4/service.py",
)


def observe_exception(error: BaseException) -> dict:
    """Read actual builtin storage, exact original class and alias topology."""
    seen = {}

    def visit(value):
        if value is None:
            return None
        if id(value) in seen:
            return {"ref": seen[id(value)]}
        if len(seen) >= 16:
            raise ValueError("controlled recovery graph exceeds bound")
        identity = len(seen)
        seen[id(value)] = identity
        args = BaseException.args.__get__(value)
        if type(args) is not tuple or any(type(a) not in (str, int) for a in args):
            raise AssertionError("unexpected original controlled args shape")
        result = {
            "id": identity,
            "type": type(value).__module__ + "." + type(value).__qualname__,
            "mro": [cls.__module__ + "." + cls.__qualname__
                    for cls in type(value).__mro__],
            "args": list(args), "message": str(value),
            "suppressed": BaseException.__suppress_context__.__get__(value),
            "cause": visit(BaseException.__cause__.__get__(value)),
            "context": visit(BaseException.__context__.__get__(value)),
        }
        if isinstance(value, OSError):
            result["os"] = {name: getattr(value, name) for name in
                            ("errno", "strerror", "filename", "filename2")}
        return result

    return visit(error)


def probe(original: Path, runtime: Path, user_data: Path, name: str, output: Path) -> None:
    """Inject one actual original class at catalog entry, without replacing catch."""
    check_original(original)
    hashes = code_hashes(original / "tobkiri_runtime")
    if code_hashes(runtime) != hashes:
        raise ValueError("staged original source differs")
    source_hashes = {}
    for relative in SOURCE_PATHS:
        actual = (runtime / relative).read_bytes()
        if actual != (original / "tobkiri_runtime" / relative).read_bytes():
            raise ValueError("recovery source differs: " + relative)
        source_hashes[relative] = hashlib.sha256(actual).hexdigest()
    sys.path.insert(0, str(runtime))
    from core_runtime.bootstrap import profile_capture as capture
    from core_runtime import active_profile_store_v4 as active
    from core_runtime import profile_definition_store_v4 as definitions
    from tobkiri_protocol import secure_persistence as secure
    from ecosystem.defaultspack.defaultspack.profile_runtime_composition import (
        install_defaultspack_profile_runtime,
    )

    for module, relative in ((capture, SOURCE_PATHS[0]), (active, SOURCE_PATHS[1]),
                             (definitions, SOURCE_PATHS[2]), (secure, SOURCE_PATHS[3])):
        if Path(module.__file__).resolve() != runtime / relative:
            raise ValueError("another original class module was imported")
    install_defaultspack_profile_runtime()
    port = capture.require_profile_runtime()
    factory = port.denied
    if type(factory) is not MethodType or factory.__self__ is not port:
        raise AssertionError("actual installed original factory method required")
    classes = {
        "secure-message": secure.SecurePersistenceError,
        "active-base": active.ActiveProfileStoreError,
        "active-integrity": active.ActiveProfileStoreIntegrityError,
        "active-lock-timeout": active.ActiveProfileStoreLockTimeout,
        "timeout-message": TimeoutError,
        "timeout-errno": TimeoutError,
        "secure-chained": secure.SecurePersistenceError,
        "active-chained": active.ActiveProfileStoreIntegrityError,
        "registry-integrity": definitions.ProfileDefinitionStoreIntegrityError,
        "registry-conflict": definitions.ProfileDefinitionStoreConflict,
    }
    args = ((errno.ETIMEDOUT, "controlled timeout") if name == "timeout-errno"
            else ("controlled recovery \u65e5\u672c\u8a9e",))
    injected = classes[name](*args)
    if name.endswith("-chained"):
        # Actual Python raise creates cause/context aliases and suppression.
        try:
            try:
                raise PermissionError(errno.EACCES, os.strerror(errno.EACCES), "fixture-entry")
            except PermissionError as cause:
                raise injected from cause
        except classes[name] as chained:
            if chained is not injected:
                raise AssertionError("original cause identity changed")
    before = filesystem(user_data)
    callables = (capture._recover_bootstrap_publication, capture.host_profile_catalog)
    reached = []

    def fail_catalog(*args, **kwargs):
        reached.append({"journal_lock_free": _lock_free(
            user_data / "profiles/.bootstrap-publication.lock")})
        raise injected

    try:
        capture.host_profile_catalog = fail_catalog
        try:
            capture._recover_bootstrap_publication(discard_superseded=True)
        except BaseException as error:
            caught = name not in {"registry-integrity", "registry-conflict"}
            if caught:
                if (port.is_resolution_denied(error) is not True
                        or error.__cause__ is not injected
                        or error.__context__ is not injected
                        or error.__suppress_context__ is not True):
                    raise AssertionError("original recovery catch/Host edges differ")
            elif error is not injected:
                raise AssertionError("uncaught registry failure was replaced")
            observation = {"outcome": "denied" if caught else "raw-error",
                           "error": observe_exception(error)}
        else:
            raise AssertionError("original recovery ignored catalog failure")
    finally:
        capture.host_profile_catalog = callables[1]
    current_factory = capture.require_profile_runtime().denied
    if (capture._recover_bootstrap_publication is not callables[0]
            or capture.host_profile_catalog is not callables[1]
            or type(current_factory) is not MethodType
            or current_factory.__self__ is not factory.__self__
            or current_factory.__func__ is not factory.__func__):
        raise AssertionError("original recovery/factory callable changed")
    if len(reached) != 1 or reached[0]["journal_lock_free"] is not False:
        raise AssertionError("actual held journal/call occurrence differs")
    if _lock_free(user_data / "profiles/.bootstrap-publication.lock") is not True:
        raise AssertionError("original finally did not release the journal")
    if code_hashes(runtime) != hashes:
        raise AssertionError("original code bytes changed")
    write_json(output, {"name": name, "expected": observation,
               "input": {"class_case": name, "args": list(args)},
               "source_hashes": source_hashes, "reached": reached,
               "user_data_before": before, "user_data_after": filesystem(user_data),
               "journal_lock_free_after": True})


def prepare(original: Path, catalog: Path, output: Path) -> None:
    """Keep each controlled original failure in a fresh process and fixture."""
    check_original(original)
    if catalog.stat().st_size > 2 * 1024 * 1024:
        raise ValueError("catalog fixtures exceed bound")
    fixtures = json.loads(catalog.read_bytes())
    if (fixtures.get("original_head") != ORIGINAL_HEAD
            or fixtures.get("case_count") != 9):
        raise ValueError("independent original catalog fixture required")
    base = catalog.parent / "packaged-valid"
    runtime = (base / "native/runtime").resolve(strict=True)
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    cases = []
    for name in CASES:
        directory = output / name
        directory.mkdir(mode=0o700)
        user_data = directory / "user-data"
        shutil.copytree(base / "seed-user-data", user_data, symlinks=True)
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", RUMI_ENVIRONMENT="production",
                   TOBKIRI_USER_DATA=str(user_data), RUMI_USER_DATA=str(user_data))
        env.pop("RUMI_APP_DIR", None)
        observation = directory / "observation.json"
        subprocess.run([sys.executable, "-B", str(Path(__file__).resolve()), "probe",
                        "--original-root", str(original), "--runtime", str(runtime),
                        "--user-data", str(user_data), "--case", name,
                        "--output", str(observation)], env=env, check=True, timeout=180)
        cases.append(json.loads(observation.read_bytes()))
    write_json(output / "summary.json", {"format": FORMAT, "original_head": ORIGINAL_HEAD,
               "case_count": len(cases), "cases": cases, "native_comparison": False,
               "qualification": False, "production_enabled": False,
               "scope": "controlled actual original cause classes at catalog entry; "
                        "unchanged recovery catch, actual denied factory and journal finally; "
                        "no actual native IO cause, Guest cause object, migrations or GUI"})
    print(json.dumps({"original_recovery_error_cases": len(cases),
                      "native_comparison": False, "qualification": False}))


def original_cause_nodes(root: dict) -> dict:
    """Project observed original identities into the closed native DATA shape."""
    tags = {
        "tobkiri_protocol.secure_persistence.SecurePersistenceError":
            "secure-persistence-error",
        "core_runtime.active_profile_store_v4.ActiveProfileStoreError":
            "active-profile-store-error",
        "core_runtime.active_profile_store_v4.ActiveProfileStoreIntegrityError":
            "active-profile-store-integrity-error",
        "core_runtime.active_profile_store_v4.ActiveProfileStoreLockTimeout":
            "active-profile-store-lock-timeout",
        "builtins.PermissionError": "permission-error",
    }
    seen, nodes = {}, []

    def visit(node):
        if node is None:
            return None
        if set(node) == {"ref"}:
            if node["ref"] not in seen:
                raise ValueError("original graph has a forward/foreign alias")
            return seen[node["ref"]]
        if (type(node.get("id")) is not int or node["id"] in seen
                or len(nodes) >= 16 or node.get("type") not in tags):
            raise ValueError("original cause is outside the finite class inventory")
        index = len(nodes)
        seen[node["id"]] = index
        nodes.append(None)
        cause, context = visit(node["cause"]), visit(node["context"])
        nodes[index] = {"kind": tags[node["type"]], "args": node["args"],
                        "fields": node.get("os", {}), "cause": cause,
                        "context": context, "suppress_context": node["suppressed"]}
        return index

    index = visit(root)
    return {"root": index, "nodes": nodes}


def compare(original: Path, fixtures: Path, observations: Path, output: Path) -> None:
    """Compare actual factory intrinsics plus native Host edges independently."""
    check_original(original)
    for path in (fixtures, observations):
        if path.stat().st_size > 2 * 1024 * 1024:
            raise ValueError("recovery comparison input exceeds bound")
    expected = json.loads(fixtures.read_bytes())
    actual = json.loads(observations.read_bytes())
    if (expected.get("format") != FORMAT or expected.get("original_head") != ORIGINAL_HEAD
            or expected.get("case_count") != 10
            or actual.get("format") != "io.tobkiri.actual-recovery-factory-observations.v1"):
        raise ValueError("fresh original/native recovery observations required")
    original_cases = {row["name"]: row for row in expected["cases"]}
    native_cases = {row["name"]: row for row in actual["cases"]}
    if (len(expected["cases"]) != 10 or set(original_cases) != set(CASES)
            or len(actual["cases"]) != 8 or set(native_cases) != set(NATIVE_CASES)
            or len(actual.get("excluded_cases", [])) != 2
            or set(actual.get("excluded_cases", [])) != set(EXCLUDED_NATIVE_CASES)):
        raise ValueError("closed recovery case inventory differs")
    lifecycle = (
        actual.get("cleanup_verified") is True
        and actual.get("source_destructor_reached") is True
        and actual.get("physical_owner_reopened") is True
        and actual.get("same_store") is True
        and type(actual.get("budget_resets")) is int and actual["budget_resets"] == 0
        and actual.get("original_deadline_preserved") is True
        and actual.get("capacity", {}).get("after_reap") == {"bytes": 0, "reservations": 0}
        and actual.get("factory_calls") == 6 and actual.get("fresh_factory_refs") == 6
        and type(actual.get("fuel", {}).get("before")) is int
        and type(actual.get("fuel", {}).get("after")) is int
        and 0 < actual["fuel"]["after"] < actual["fuel"]["before"] <= 2_000_000_000
        and type(actual.get("inspection_attempts", {}).get("before")) is int
        and actual["inspection_attempts"]["before"] == actual["inspection_attempts"].get("after")
    )
    rows = []
    for name in NATIVE_CASES:
        reference, row = original_cases[name]["expected"], native_cases[name]
        error = reference["error"]
        if reference["outcome"] == "raw-error":
            wanted = {"outcome": "raw-error", "factory_called": False,
                      "raw_error": {"class": error["type"], "message": error["message"],
                                    "args": error["args"]}}
            observed = {key: row.get(key) for key in wanted}
        else:
            # The original Host raise has both edges to the same actual cause.
            # The Guest factory returns an untouched fresh exception instead;
            # its separate native Host edges must reconstruct that meaning.
            if (error["type"] != "ecosystem.defaultspack.domain.runtime_v4.service.ProfileResolutionDenied"
                    or error["context"] != {"ref": error["cause"]["id"]}
                    or error["suppressed"] is not True):
                raise AssertionError("original Host explicit-from contract differs")
            wanted_graph = original_cause_nodes(error["cause"])
            cause = row.get("native_cause", {})
            graph = cause.get("details", {})
            if (set(graph) != {"format", "graph_id", "root", "nodes"}
                    or graph.get("format") != "tobkiri.native-exception/1"
                    or type(graph.get("graph_id")) is not int or graph["graph_id"] <= 0):
                raise ValueError("actual native recovery graph envelope differs")
            wanted = {"outcome": "denied", "factory_called": True,
                      "factory_kind": "profile-resolution-denied",
                      "factory_denied": {"args": error["args"], "cause": None,
                                         "context": None, "suppress_context": False},
                      "host_cause_context_same": True, "host_suppressed": True,
                      "cause_code": wanted_graph["nodes"][0]["kind"],
                      "cause_message": error["cause"]["message"],
                      "cause_graph": wanted_graph}
            observed = {key: row.get(key) for key in
                        ("outcome", "factory_called", "factory_kind", "factory_denied",
                         "host_cause_context_same", "host_suppressed")}
            observed.update(cause_code=cause.get("code"), cause_message=cause.get("message"),
                            cause_graph={key: graph.get(key) for key in ("root", "nodes")})
        difference = first_difference(wanted, observed)
        rows.append({"name": name, "difference": difference,
                     "matched": difference is None and lifecycle})
    passed = sum(row["matched"] for row in rows)
    report = {"format": "io.tobkiri.actual-recovery-factory-comparison.v1",
              "original_head": ORIGINAL_HEAD, "cases": rows, "passed": passed,
              "failed": len(rows) - passed, "lifecycle_verified": lifecycle,
              "excluded_cases": list(EXCLUDED_NATIVE_CASES), "qualification": False,
              "production_enabled": False,
              "scope": "eight controlled native-origin class graphs plus actual same-Store "
                       "Guest denied returns and Host edges; no native IO origin, actual custom "
                       "Guest cause object, factory throw, catalog, migration or GUI qualification"}
    write_json(output, report)
    if passed != 8:
        raise AssertionError({"lifecycle_verified": lifecycle, "failures":
                              [row for row in rows if not row["matched"]]})
    print(json.dumps({"native_recovery_cases": passed, "qualification": False}))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prepare", "probe", "compare"))
    parser.add_argument("--original-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--catalog-fixtures", type=Path)
    parser.add_argument("--runtime", type=Path)
    parser.add_argument("--user-data", type=Path)
    parser.add_argument("--case", choices=CASES)
    parser.add_argument("--fixtures", type=Path)
    parser.add_argument("--observations", type=Path)
    args = parser.parse_args()
    if args.command == "prepare":
        if args.catalog_fixtures is None:
            parser.error("prepare requires --catalog-fixtures")
        prepare(args.original_root.resolve(strict=True),
                args.catalog_fixtures.resolve(strict=True), args.output.resolve())
    elif args.command == "probe":
        if args.runtime is None or args.user_data is None or args.case is None:
            parser.error("probe requires --runtime, --user-data and --case")
        probe(args.original_root.resolve(strict=True), args.runtime.resolve(strict=True),
              args.user_data.resolve(strict=True), args.case, args.output.resolve())
    else:
        if args.fixtures is None or args.observations is None:
            parser.error("compare requires --fixtures and --observations")
        compare(args.original_root.resolve(strict=True), args.fixtures.resolve(strict=True),
                args.observations.resolve(strict=True), args.output.resolve())


if __name__ == "__main__":
    main()
