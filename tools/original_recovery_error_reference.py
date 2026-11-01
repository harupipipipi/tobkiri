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

from original_authority_reference import ORIGINAL_HEAD, write_json
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
                raise PermissionError(errno.EACCES, "controlled read", "fixture-entry")
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


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prepare", "probe"))
    parser.add_argument("--original-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--catalog-fixtures", type=Path)
    parser.add_argument("--runtime", type=Path)
    parser.add_argument("--user-data", type=Path)
    parser.add_argument("--case", choices=CASES)
    args = parser.parse_args()
    if args.command == "prepare":
        if args.catalog_fixtures is None:
            parser.error("prepare requires --catalog-fixtures")
        prepare(args.original_root.resolve(strict=True),
                args.catalog_fixtures.resolve(strict=True), args.output.resolve())
    else:
        if args.runtime is None or args.user_data is None or args.case is None:
            parser.error("probe requires --runtime, --user-data and --case")
        probe(args.original_root.resolve(strict=True), args.runtime.resolve(strict=True),
              args.user_data.resolve(strict=True), args.case, args.output.resolve())


if __name__ == "__main__":
    main()
