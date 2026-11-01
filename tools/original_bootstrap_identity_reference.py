"""Observe the unchanged original bootstrap getter and canonical validator in CI.

Controlled cases replace only the raw getter on the already installed original
port. The original require/validator/denied factory and Host raise are executed
unchanged. This is a finite oracle, not full publication recovery or a product
route. The unmodified default getter is observed separately before/after cases.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
from pathlib import Path
import struct
import sys

from original_authority_reference import ORIGINAL_HEAD, write_json
from original_setup_review_reference import check_original


FORMAT = "io.tobkiri.original-bootstrap-identity-reference.v1"
SOURCES = (
    "core_runtime/bootstrap/profile_capture.py",
    "core_runtime/profile_runtime_port.py",
    "tobkiri_protocol/ids.py",
    "tobkiri_protocol/errors.py",
    "ecosystem/defaultspack/defaultspack/profile_runtime_composition.py",
    "ecosystem/defaultspack/domain/profile_resolution.py",
)


def scalar(value: object) -> dict:
    """Encode only exact builtin exception scalars, including surrogate strings."""
    cls = type(value)
    if value is None:
        return {"kind": "none"}
    if cls is bool:
        return {"kind": "bool", "value": value}
    if cls is int:
        return {"kind": "int", "value": str(value)}
    if cls is float:
        return {"kind": "float", "bits": struct.pack(">d", value).hex()}
    if cls is str:
        return {"kind": "str", "codepoints": [ord(c) for c in value]}
    raise ValueError("oracle exception arguments must be exact builtin scalars")


def exception_graph(error: BaseException, classify) -> dict:
    """Observe real builtin edges once, retaining aliases and cycles as references."""
    seen: dict[int, int] = {}

    def visit(current):
        if current is None:
            return None
        key = id(current)
        if key in seen:
            return {"ref": seen[key]}
        if len(seen) == 16:
            raise ValueError("fixed exception graph exceeded oracle bound")
        number = len(seen)
        seen[key] = number
        args = BaseException.args.__get__(current)
        node = {"id": number, "kind": classify(current),
                "args": [scalar(v) for v in args],
                "suppress_context": BaseException.__suppress_context__.__get__(current),
                "cause": visit(BaseException.__cause__.__get__(current)),
                "context": visit(BaseException.__context__.__get__(current))}
        if isinstance(current, OSError):
            node["os_fields"] = {
                name: scalar(getattr(OSError, name).__get__(current))
                for name in ("errno", "strerror", "filename", "filename2")
            }
        return node

    return visit(error)


def returned_value(value: object) -> dict:
    """Only top-level builtin kind is needed before the original type rejection."""
    names = {type(None): "none", bool: "bool", int: "int", float: "float",
             str: "str", list: "list", tuple: "tuple", dict: "dict", set: "set",
             frozenset: "frozenset"}
    kind = names.get(type(value), "inventory-out")
    result = {"kind": kind}
    if type(value) is str:
        result["length"] = len(value)
        # Mirror only the old >128 reachpoint; no clipping of a valid-length ID.
        result["codepoints"] = [ord(c) for c in value] if len(value) <= 128 else []
    return result


def return_cases() -> tuple[tuple[str, object], ...]:
    """Keep ordering-sensitive Unicode/type inputs independent of the native code."""
    recursive = []
    recursive.append(recursive)
    return (
        ("defaults", "defaults"), ("single-letter", "a"),
        ("digits-after-first", "a0129"), ("all-separators", "a.b-c_d"),
        ("length-128", "a" * 128), ("length-129", "a" * 129),
        ("long-uppercase-decomposed", "A\u0301" * 65),
        ("empty", ""), ("none", None), ("false", False), ("true", True),
        ("int-zero", 0), ("int-large", 1 << 4097), ("float-nan", float("nan")),
        ("list-empty", []), ("list-cyclic", recursive),
        ("tuple-nested", (object(),)), ("dict-custom-content", {"x": object()}),
        ("set", {1}), ("frozenset", frozenset({1})),
        ("upper", "Defaults"), ("upper-i-dot", "\u0130"),
        ("sigma-alone", "\u03a3"), ("sigma-final", "\u039f\u03a3"),
        ("lower-greek", "\u03bf\u03c2"),
        ("nfc-upper", "\u00c5"), ("nfd-upper", "A\u030a"),
        ("nfc-lower", "\u00e9"), ("nfd-lower", "e\u0301"),
        ("hangul-jamo", "\u1100\u1161\u11a8"), ("hangul-composed", "\uac01"),
        ("composition-excluded", "\u0344"),
        ("combining-order", "a\u0315\u0300"),
        ("rumi-prefix", "rumi.a"), ("rumiai-prefix", "rumiai.a"),
        ("viewer-prefix", "viewer.a"), ("legacy-prefix", "legacy.a"),
        ("legacy-without-dot", "legacy"), ("prefix-case-priority", "Rumi.a"),
        ("leading-digit", "1a"), ("leading-dot", ".a"),
        ("trailing-dot", "a."), ("double-separator", "a.-b"),
        ("slash", "a/b"), ("backslash", "a\\b"),
        ("space", "a b"), ("newline", "a\n"), ("nul", "a\0"),
        ("single-quote", "a'b"), ("double-quote", 'a"b'),
        ("both-quotes", "a'\"b"), ("tab", "a\tb"),
        ("surrogate-high", "a\ud800"), ("surrogate-low", "\udfff"),
        ("surrogate-pair", "\ud83e\udded"), ("nonbmp", "a\U0001f9ed"),
        ("unassigned", "a\u0378"), ("unicode15-1", "a\U0002ebf0"),
    )


def raised_cases() -> tuple[tuple[str, BaseException], ...]:
    """Actual exceptions exercise catch ancestry separately from portable labels."""
    chained = ValueError("outer")
    inner = TypeError("inner")
    chained.__cause__ = inner
    chained.__context__ = inner
    cyclic = RuntimeError("cycle")
    cyclic.__context__ = cyclic
    return (
        ("value-error", ValueError("raw getter failed")),
        ("value-error-empty", ValueError()),
        ("value-error-scalars", ValueError(None, False, 19, -0.0, "\ud800")),
        ("type-error", TypeError("raw getter failed")),
        ("key-error", KeyError("profile_id")),
        ("runtime-error", RuntimeError("raw getter failed")),
        ("os-error", OSError(5, "io", "first", None, "second")),
        ("permission-error", PermissionError(13, "denied", "first")),
        ("file-not-found", FileNotFoundError(2, "absent", "first")),
        ("value-error-intrinsic-chain", chained),
        ("runtime-error-context-cycle", cyclic),
    )


@contextmanager
def controlled_getter(port, value: object, raised: bool):
    """Keep the original installed instance and restore its exact class method."""
    cls = type(port)
    previous = cls.__dict__["bootstrap_profile_id"]
    calls = []

    def getter(self):
        if self is not port:
            raise AssertionError("controlled getter called on another port")
        calls.append("raw-getter")
        if raised:
            raise value
        return value

    cls.bootstrap_profile_id = getter
    try:
        yield calls
    finally:
        cls.bootstrap_profile_id = previous


def prepare(original: Path, destination: Path) -> None:
    """Run the actual original Host helper; expected outcomes are never hand-coded."""
    check_original(original)
    runtime = original / "tobkiri_runtime"
    sys.path.insert(0, str(runtime))
    from core_runtime.bootstrap import profile_capture
    from core_runtime.profile_runtime_port import require_profile_runtime
    from ecosystem.defaultspack.defaultspack.profile_runtime_composition import (
        install_defaultspack_profile_runtime,
    )
    from tobkiri_protocol.errors import ProtocolError

    install_defaultspack_profile_runtime()
    port = require_profile_runtime()
    original_getter = type(port).__dict__["bootstrap_profile_id"]
    before = profile_capture._bootstrap_profile_id()
    if before != "defaults":
        raise AssertionError("unmodified original bootstrap getter changed")

    def classify(error):
        if port.is_resolution_denied(error):
            return "profile-resolution-denied"
        if type(error) is ProtocolError:
            return "protocol-error"
        kinds = {ValueError: "value-error", TypeError: "type-error", KeyError: "key-error",
                 RuntimeError: "runtime-error", OSError: "os-error",
                 PermissionError: "permission-error", FileNotFoundError: "file-not-found-error"}
        try:
            return kinds[type(error)]
        except KeyError as missing:
            raise ValueError("unknown oracle exception class") from missing

    results = []
    inventory = [(name, value, False) for name, value in return_cases()]
    inventory.extend((name, value, True) for name, value in raised_cases())
    if len({name for name, _, _ in inventory}) != len(inventory):
        raise AssertionError("duplicate controlled input")
    for name, value, raised in inventory:
        raw = ({"outcome": "getter-raised", "recovery_catch":
                "os-error" if isinstance(value, OSError) else
                "value-error" if isinstance(value, ValueError) else "none",
                "error": exception_graph(value, classify)} if raised else
               {"outcome": "getter-returned", "value": returned_value(value)})
        with controlled_getter(port, value, raised) as calls:
            try:
                result = profile_capture._bootstrap_profile_id()
                observed = {"outcome": "returned", "value": scalar(result),
                            "same_value": result is value}
            except Exception as error:
                observed = {"outcome": "error", "error": exception_graph(error, classify),
                            "same_raw_error": raised and error is value}
        if calls != ["raw-getter"] or require_profile_runtime() is not port:
            raise AssertionError("original helper getter count/composition identity changed")
        results.append({"name": name, "raw": raw, "host": observed})
    if (type(port).__dict__["bootstrap_profile_id"] is not original_getter
            or profile_capture._bootstrap_profile_id() != before):
        raise AssertionError("controlled getter restoration failed")
    destination.mkdir(parents=True, mode=0o700, exist_ok=False)
    report = {"format": FORMAT, "original_head": ORIGINAL_HEAD, "python": "3.13.16",
              "sources": {name: hashlib.sha256((runtime / name).read_bytes()).hexdigest()
                          for name in SOURCES},
              "unmodified_default_getter_before_and_after": before,
              "controlled_raw_getter_only": True, "installed_port_identity_preserved": True,
              "cases": results, "reference_checks_passed": True,
              "native_comparison": False, "qualification": False,
              "production_enabled": False,
              "scope": "finite original _bootstrap_profile_id only; controlled getter "
                       "inputs, actual validator/factory/raise; no publication guard, "
                       "recovery catch, actual component or product/GUI qualification"}
    write_json(destination / "summary.json", report)
    print({"original_bootstrap_identity_cases": len(results), "native_comparison": False,
           "qualification": False, "production_enabled": False})


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--original-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    prepare(args.original_root.resolve(strict=True), args.output.resolve())


if __name__ == "__main__":
    main()
