"""CI-only reference for the actual App schema caller, not a new validator.

Controlled dependency faults are caller-contract fixtures, never Host/Gate or
source-admission proof. The real corpus uses the genuine native path reader.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import importlib
import json
import os
from pathlib import Path
import sys

HERE = Path(__file__).resolve().parent
FORMAT = "tobkiri.app-schema-caller-reference/1"
MAX_OUTPUT = 4 * 1024 * 1024


def require_ci() -> None:
    """Keep product/dependency execution off the capacity-constrained host."""
    if (sys.platform != "linux" or sys.version_info[:3] != (3, 13, 16)
            or os.environ.get("GITHUB_ACTIONS") != "true"):
        raise RuntimeError("App schema reference requires pinned Linux CI")


def encoded(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True,
                      separators=(",", ":")).encode()


def digest(value: object) -> str:
    return hashlib.sha256(encoded(value)).hexdigest()


def ordered_value(value: object) -> object:
    """Retain recursive object iteration order as explicit comparison DATA."""
    if isinstance(value, dict):
        return {"object": [[key, ordered_value(item)] for key, item in value.items()]}
    if isinstance(value, list):
        return {"array": [ordered_value(item) for item in value]}
    return {"scalar": value}


def physical(path: Path) -> None:
    if not path.is_absolute() or path.resolve(strict=True) != path:
        raise ValueError("physical absolute oracle path required")


def boundary_values() -> list:
    """Read fixture DATA without importing or invoking the library oracle."""
    tree = ast.parse((HERE / "schema_kernel_oracle.py").read_bytes())
    values = [ast.literal_eval(node.value) for node in ast.walk(tree)
              if isinstance(node, ast.Assign)
              and any(isinstance(target, ast.Name) and target.id == "curated"
                      for target in node.targets)]
    if len(values) != 1 or len(values[0]) != 22:
        raise ValueError("complete independent library boundary DATA required")
    return values[0]


def fixture_bytes() -> dict[str, bytes]:
    """Declare input DATA, preserving the original boundary key order."""
    files = {"boundary-" + name + ".schema.json": json.dumps(
        value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode()
             for name, value in boundary_values()}
    files.update({
        "valid.schema.json": b'{"$id":"urn:caller:valid","type":"object"}',
        "other.schema.json": b'{"$id":"urn:caller:other","type":"string"}',
        "malformed.schema.json": b'{',
        "invalid-utf8.schema.json": b'\xff',
        "duplicate.schema.json": b'{"type":"object","type":"string"}',
        "nonfinite.schema.json": b'{"minimum":NaN}',
    })
    return files


def prepare(destination: Path) -> None:
    """Write small shared real-file fixtures, outside all protocol source roots."""
    require_ci()
    physical(destination.parent)
    if os.path.lexists(destination):
        raise ValueError("new fixture directory required")
    destination.mkdir(mode=0o700)
    files = fixture_bytes()
    for name, raw in files.items():
        (destination / name).write_bytes(raw)
    store = destination / "store"
    store.mkdir(mode=0o700)
    for name in ("valid.schema.json", "other.schema.json"):
        (store / name).write_bytes(files[name])
    (destination / "fixture-index.json").write_bytes(encoded({
        name: {"bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}
        for name, raw in files.items()
    }))


def fixture_inventory(root: Path) -> dict:
    physical(root)
    physical(root / "fixture-index.json")
    values = json.loads((root / "fixture-index.json").read_bytes())
    declared = {name: {"bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}
                for name, raw in fixture_bytes().items()}
    if type(values) is not dict or encoded(values) != encoded(declared):
        raise ValueError("complete caller fixture inventory required")
    expected = set(values) | {"fixture-index.json", "store/valid.schema.json",
                              "store/other.schema.json"}
    paths = list(root.rglob("*"))
    if (any(path.is_symlink() for path in paths)
            or {path.relative_to(root).as_posix() for path in paths if path.is_file()}
            != expected):
        raise ValueError("caller fixture file closure differs")
    for name, expected in values.items():
        path = root / name
        physical(path)
        raw = path.read_bytes()
        if ({"bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}
                != expected):
            raise ValueError("caller fixture source differs")
    for name in ("valid.schema.json", "other.schema.json"):
        path = root / "store" / name
        physical(path)
        if path.read_bytes() != (root / name).read_bytes():
            raise ValueError("store fixture differs from its original member")
    return values


def type_name(value: object) -> str:
    return type(value).__module__ + "." + type(value).__qualname__


def verify_cases(cases: dict) -> None:
    """Refuse a broken observer instead of calling two broken lanes parity."""
    for name, case in cases.items():
        if name in {"cache", "store", "failure-cache"}:
            continue
        if (case["checked_count"] > 1 or case["events"].count("select") > 1
                or (case["checked_count"] and not case["checker_received_parser_object"])):
            raise ValueError("actual selected checker call/argument identity differs")
    ignored = cases["ignored-return"]
    if (ignored["events"] != ["read:valid.schema.json", "parse", "select", "call"]
            or ignored["outcome"].get("same_payload") is not True):
        raise ValueError("checker return was used or caller order differs")
    for name in ("missing", "read-error", "parse-error", "lookup-error"):
        if cases[name]["checked_count"] != 0:
            raise ValueError("checker ran after an earlier caller failure")
    for name in ("checker-error", "read-error", "parse-error", "lookup-error"):
        if cases[name]["outcome"]["cause_is_supplied"] is not True:
            raise ValueError("actual caught cause object was replaced")
    for name in ("malformed", "invalid-utf8", "duplicate", "nonfinite"):
        if cases["parse/" + name]["outcome"]["cause_is_parser_error"] is not True:
            raise ValueError("actual strict parser cause object was replaced")
    if cases["format-error"]["outcome"]["context_is_supplied"] is not True:
        raise ValueError("exception formatter failure lost its handled context")
    if cases["base-error"]["outcome"]["type"] != type_name(Abort()):
        raise ValueError("BaseException was wrapped as a schema error")
    for name in ("boolean-true", "boolean-false", "null-schema", "fractional-count",
                 "integral-float-count", "large-exact-count"):
        if cases["boundary/" + name]["checked_count"] != 0:
            raise ValueError("strict/object rejection incorrectly reached the library")
    for key, value in cases["cache"].items():
        if key != "cache" and value is not True:
            raise ValueError("actual name-key cache identity/skip behavior differs")
    if any(value is not True for value in cases["store"].values()):
        raise ValueError("schema/store cache object sharing or independence differs")
    if any(value is not True for value in cases["failure-cache"].values()):
        raise ValueError("failed loads were cached or successful retry was not cached")


class CheckerFailure(Exception):
    pass


class FormatFailure(Exception):
    pass


class UnformattableFailure(Exception):
    def __str__(self) -> str:
        raise FormatFailure("exception formatting failed")


class Abort(BaseException):
    pass


class IgnoredReturn:
    def __bool__(self) -> bool:
        raise AssertionError("the checker return value must not be inspected")

    def __str__(self) -> str:
        raise AssertionError("the checker return value must not be formatted")


def caller_cases(module: object, fixtures: Path) -> dict:
    """Execute original callable selection, failures and separate actual caches."""
    from jsonschema import exceptions
    from schema_kernel_observation import meta_nodes, observe

    original = (module.SCHEMA_DIR, module.Draft202012Validator, module.strict_loads,
                Path.read_bytes)
    aliases = module.SCHEMA_ALIASES
    alias = "__caller_oracle_alias__"
    if alias in aliases:
        raise ValueError("caller fixture alias conflicts with the real namespace")
    known = meta_nodes()
    cases = {}

    def reset() -> None:
        module.load_schema.cache_clear()
        module._schema_store.cache_clear()
        module.SCHEMA_DIR, module.Draft202012Validator, module.strict_loads = original[:3]
        Path.read_bytes = original[3]
        aliases.pop(alias, None)

    def one(name: str, filename: str, mode: str = "default") -> None:
        reset()
        module.SCHEMA_DIR = fixtures
        events, parsed, checked, parser_errors = [], [], [], []
        supplied = (CheckerFailure("checker failed") if mode == "checker-error" else
                    UnformattableFailure() if mode == "format-error" else
                    Abort("abort checker") if mode == "base-error" else
                    LookupError("checker lookup failed") if mode == "lookup-error" else
                    OSError("reader failed") if mode == "read-error" else
                    module.SchemaValidationError("parser failed")
                    if mode == "parse-error" else None)

        def read(path: Path) -> bytes:
            events.append("read:" + path.name)
            if mode == "read-error":
                raise supplied
            return original[3](path)

        def parse(raw: bytes) -> object:
            events.append("parse")
            try:
                if mode == "parse-error":
                    raise supplied
                value = original[2](raw)
            except BaseException as error:
                parser_errors.append(error)
                raise
            parsed.append(value)
            return value

        def check(payload: object) -> object:
            events.append("call")
            checked.append(payload)
            if mode in {"checker-error", "format-error", "base-error"}:
                raise supplied
            if mode == "ignored-return":
                return IgnoredReturn()
            return original[1].check_schema(payload)

        class Selected:
            @property
            def check_schema(self):
                events.append("select")
                if mode == "lookup-error":
                    raise supplied
                return check

        module.strict_loads = parse
        module.Draft202012Validator = None if mode == "missing" else Selected()
        Path.read_bytes = read
        try:
            result = module.load_schema(filename)
        except BaseException as error:
            cause = error.__cause__
            outcome = {
                "type": type_name(error), "message": str(error),
                "code": getattr(error, "code", None),
                "diagnostics": list(getattr(error, "diagnostics", ())),
                "cause_type": type_name(cause) if cause is not None else None,
                "context_type": (type_name(error.__context__)
                                 if error.__context__ is not None else None),
                "cause_is_supplied": supplied is not None and cause is supplied,
                "context_is_supplied": supplied is not None and error.__context__ is supplied,
                "cause_is_parser_error": bool(parser_errors) and cause is parser_errors[0],
                "suppress_context": error.__suppress_context__,
            }
            if isinstance(cause, exceptions.SchemaError):
                outcome["schema_error"] = observe(cause, parsed[0], known)
        else:
            outcome = {"value": ordered_value(result), "same_payload": result is parsed[0]}
        cases[name] = {
            "events": events, "checked_count": len(checked),
            "checker_received_parser_object": bool(checked) and checked[0] is parsed[0],
            "outcome": outcome,
        }

    try:
        for name, _ in boundary_values():
            one("boundary/" + name, "boundary-" + name + ".schema.json")
        for name in ("malformed", "invalid-utf8", "duplicate", "nonfinite"):
            one("parse/" + name, name + ".schema.json")
        for mode in ("missing", "checker-error", "format-error", "base-error",
                     "lookup-error", "read-error", "parse-error", "ignored-return"):
            one(mode, "valid.schema.json", mode)
        reset()
        module.SCHEMA_DIR = fixtures
        aliases[alias] = "valid.schema.json"
        first = module.load_schema(alias)
        filename_value = module.load_schema("valid.schema.json")
        keyword_value = module.load_schema(schema_name=alias)
        first["__caller_mutation__"] = "retained"
        aliases[alias] = "other.schema.json"
        module.Draft202012Validator = None

        def forbidden_read(path: Path) -> bytes:
            raise AssertionError("a cache hit must not read the changed source")

        Path.read_bytes = forbidden_read
        hit = module.load_schema(alias)
        cases["cache"] = {
            "same_mutated_object": hit is first and hit["__caller_mutation__"] == "retained",
            "filename_key_distinct": first is not filename_value,
            "keyword_key_distinct": first is not keyword_value,
            "hit_ignores_changed_alias_reader_dependency": hit is first,
            "cache": module.load_schema.cache_info()._asdict(),
        }
        reset()
        module.SCHEMA_DIR = fixtures
        retry_calls = []
        retry_error = CheckerFailure("first check fails")

        class RetrySelected:
            @staticmethod
            def check_schema(payload: object) -> None:
                retry_calls.append(payload)
                if len(retry_calls) == 1:
                    raise retry_error
                original[1].check_schema(payload)

        module.Draft202012Validator = RetrySelected
        try:
            module.load_schema("valid.schema.json")
        except module.SchemaValidationError as error:
            same_cause = error.__cause__ is retry_error
        else:
            raise ValueError("first real dependency fault did not escape")
        retried = module.load_schema("valid.schema.json")
        cached = module.load_schema("valid.schema.json")
        cases["failure-cache"] = {
            "original_cause": same_cause,
            "fresh_retry_parser_object": (len(retry_calls) == 2
                                          and retry_calls[0] is not retry_calls[1]),
            "retry_returns_actual_checked_object": retried is retry_calls[-1],
            "success_then_cached": cached is retried and len(retry_calls) == 2,
        }
        reset()
        module.SCHEMA_DIR = fixtures / "store"
        store = module._schema_store()
        old = module.load_schema("valid.schema.json")
        module.load_schema.cache_clear()
        fresh = module.load_schema("valid.schema.json")
        cases["store"] = {
            "same_stored_object": store["urn:caller:valid"] is old,
            "clearing_schema_cache_does_not_clear_store": module._schema_store() is store,
            "fresh_load_distinct": fresh is not old,
            "store_retains_old_object": store["urn:caller:valid"] is old,
        }
    finally:
        reset()
    verify_cases(cases)
    return cases


def run(source: Path, source_kind: str, inputs: Path, fixtures: Path,
        bundle_source: Path, output: Path) -> None:
    """Import only verified real code, then record actual caller observations."""
    require_ci()
    import fetch_schema
    from bundle_catalog_oracle import SOURCE_HASHES, LOCK_HASH, COLLECTIONS

    for path in (source, inputs, fixtures, bundle_source, output.parent):
        physical(path)
    if os.path.lexists(output) or output.suffix != ".json":
        raise ValueError("new bounded external oracle output required")
    pins = fixture_inventory(fixtures)
    fetch_schema.verify(inputs)
    for name in SOURCE_HASHES:
        physical(source / "tobkiri_protocol" / name)
    source_hashes = {name: hashlib.sha256(
        (source / "tobkiri_protocol" / name).read_bytes()).hexdigest()
        for name in SOURCE_HASHES}
    if source_kind == "original" and source_hashes != SOURCE_HASHES:
        raise ValueError("pinned original App source differs")
    if source_kind == "current" and source != HERE.parents[1] / "tobkiri_runtime":
        raise ValueError("current App source must be this checked-out repository")
    if any(name == "tobkiri_protocol" or name.startswith("tobkiri_protocol.")
           for name in sys.modules):
        raise ValueError("App protocol was imported before source checks")
    records = json.loads((inputs / "schema-inputs.json").read_bytes())["inputs"]
    packages = [inputs / "packages" / (
        "rpds-py-native" if row["role"] == "native-oracle" else row["distribution"])
        for row in records]
    sys.path[:0] = [str(source), *(str(path) for path in packages)]
    module = importlib.import_module("tobkiri_protocol.validation")
    if Path(module.__file__).resolve(strict=True) != source / "tobkiri_protocol/validation.py":
        raise ValueError("actual App module origin differs")
    bundle = importlib.import_module("tobkiri_protocol.bundle_catalog")
    root = bundle_source / "ecosystem/defaultspack/v4"
    lock = (root / "bundle.lock.json").read_bytes()
    if hashlib.sha256(lock).hexdigest() != LOCK_HASH:
        raise ValueError("full original bundle source differs")
    entry_paths = {entry["path"] for entry in json.loads(lock)["entries"]}
    if len(entry_paths) != 305:
        raise ValueError("complete bundle entry inventory required")
    module.load_schema.cache_clear()
    module._schema_store.cache_clear()
    original_read = Path.read_bytes
    entry_reads, schema_reads = [], []

    def read_actual(path: Path) -> bytes:
        if path.parent == module.SCHEMA_DIR:
            schema_reads.append(path.name)
        try:
            relative = path.relative_to(root).as_posix()
        except ValueError:
            pass
        else:
            if relative in entry_paths:
                entry_reads.append(relative)
        return original_read(path)

    Path.read_bytes = read_actual
    try:
        catalog = bundle.BundledCatalog.load(root)
    finally:
        Path.read_bytes = original_read
    if len(entry_reads) != 453 or len(schema_reads) != 45:
        raise ValueError("actual full bundle/schema read sequence was narrowed or changed")
    full = {
        "collections": {name: {"count": len(getattr(catalog, name)),
                               "sha256": digest(ordered_value(getattr(catalog, name)))}
                        for name in COLLECTIONS},
        "schema_cache": module.load_schema.cache_info()._asdict(),
        "store_cache": module._schema_store.cache_info()._asdict(),
        "entry_reads": entry_reads, "schema_reads": schema_reads,
    }
    if full["schema_cache"]["misses"] != 45 or full["schema_cache"]["currsize"] != 45:
        raise ValueError("actual full-catalog selfcheck cache misses changed")
    module.load_schema.cache_clear()
    module._schema_store.cache_clear()
    names = sorted(path.name for path in module.SCHEMA_DIR.glob("*.schema.json"))
    if len(names) != 42:
        raise ValueError("complete original schema inventory required")
    actual = {name: digest(ordered_value(module.load_schema(name))) for name in names}
    observations = caller_cases(module, fixtures)
    if fixture_inventory(fixtures) != pins:
        raise ValueError("shared caller fixtures changed during reference execution")
    if source_hashes != {name: hashlib.sha256(
            (source / "tobkiri_protocol" / name).read_bytes()).hexdigest()
            for name in SOURCE_HASHES}:
        raise ValueError("actual App source changed during reference execution")
    result = {
        "format": FORMAT, "source_kind": source_kind, "source_hashes": source_hashes,
        "tool_head": os.environ.get("GITHUB_SHA"),
        "qualification": False, "production_enabled": False,
        "scope": "native App caller contract only; no Wasm budget/GUI/OS-owner proof",
        "fixtures": pins,
        "contract": {"full_catalog": full, "original_42": actual, "cases": observations},
    }
    raw = encoded(result)
    if len(raw) > MAX_OUTPUT:
        raise ValueError("caller reference exceeds its data-only bound")
    with output.open("xb") as stream:
        stream.write(raw + b"\n")


def compare(original: Path, current: Path, output: Path) -> None:
    """Compare actual lanes without converting library vectors into App ones."""
    require_ci()
    values, raw_inputs = [], []
    for path, kind in ((original, "original"), (current, "current")):
        physical(path)
        if not path.is_file() or path.stat().st_size > MAX_OUTPUT:
            raise ValueError("bounded actual caller report required")
        raw = path.read_bytes()
        value = json.loads(raw)
        if (value.get("format") != FORMAT or value.get("source_kind") != kind
                or value.get("qualification") is not False
                or value.get("production_enabled") is not False):
            raise ValueError("caller reference identity or scope differs")
        verify_cases(value["contract"]["cases"])
        values.append(value)
        raw_inputs.append(raw)
    equal = (encoded(values[0]["fixtures"]) == encoded(values[1]["fixtures"])
             and encoded(values[0]["contract"]) == encoded(values[1]["contract"]))
    physical(output.parent)
    if os.path.lexists(output):
        raise ValueError("new comparison receipt required")
    output.write_bytes(encoded({
        "format": "tobkiri.app-schema-caller-comparison/1",
        "matched": equal, "qualification": False, "production_enabled": False,
        "scope": "native caller contract only; no replacement/Wasm-budget/GUI proof",
        "original_report_sha256": hashlib.sha256(raw_inputs[0]).hexdigest(),
        "current_report_sha256": hashlib.sha256(raw_inputs[1]).hexdigest(),
        "actual_fixture_cases": len(values[0]["contract"]["cases"]),
        "original_schemas": len(values[0]["contract"]["original_42"]),
    }) + b"\n")
    if not equal:
        raise ValueError("actual App caller contracts differ; inspect both original reports")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    stage = commands.add_parser("prepare")
    stage.add_argument("destination", type=Path)
    oracle = commands.add_parser("run")
    oracle.add_argument("--source-kind", choices=("original", "current"), required=True)
    for name in ("source", "inputs", "fixtures", "bundle-source", "output"):
        oracle.add_argument("--" + name, type=Path, required=True)
    comparison = commands.add_parser("compare")
    for name in ("original", "current", "output"):
        comparison.add_argument("--" + name, type=Path, required=True)
    args = vars(parser.parse_args())
    command = args.pop("command")
    {"prepare": prepare, "run": run, "compare": compare}[command](**args)
