"""Best-effort bounded init failure observation through the fixed native import.

Only exact builtin exception descriptors are inspected. This DATA is neither a
successful registry nor filesystem authority, and native traps need not reach
Python's exception machinery or the observer.
"""

from __future__ import annotations

import ordered_json_v2 as wire

from .value_graph import Scope

FORMAT = "io.tobkiri.native-init-diagnostic.v1"
MAX_BYTES = 4096
_SAFE_INTEGER = (1 << 53) - 1
_KINDS = (
    (ValueError, "value-error"),
    (TypeError, "type-error"),
    (RuntimeError, "runtime-error"),
    (NameError, "name-error"),
    (ImportError, "import-error"),
    (ModuleNotFoundError, "module-not-found-error"),
    (KeyError, "key-error"),
    (AttributeError, "attribute-error"),
    (AssertionError, "assertion-error"),
    (MemoryError, "memory-error"),
    (RecursionError, "recursion-error"),
    (OSError, "os-error"),
    (FileNotFoundError, "file-not-found-error"),
    (PermissionError, "permission-error"),
)
_FIELDS = {
    NameError: ((NameError, "name"),),
    ImportError: ((ImportError, "name"), (ImportError, "path")),
    ModuleNotFoundError: ((ImportError, "name"), (ImportError, "path")),
    AttributeError: ((AttributeError, "name"),),
    **{
        cls: tuple((OSError, name) for name in (
            "errno", "strerror", "filename", "filename2",
        ))
        for cls in (OSError, FileNotFoundError, PermissionError)
    },
}


def _text(value: object, maximum: int) -> bool:
    if type(value) is not str or len(value) > maximum:
        return False
    try:
        return len(value.encode("utf-8")) <= maximum
    except UnicodeEncodeError:
        return False


def _scalar(value: object) -> bool:
    return (
        value is None or type(value) is bool
        or (type(value) is int and -_SAFE_INTEGER <= value <= _SAFE_INTEGER)
        or _text(value, 512)
    )


def _minimal(scope: Scope, phase: str, kind: str) -> dict:
    return {
        "format": FORMAT,
        "scope": scope.wire(),
        "phase": phase,
        "kind": kind,
        "args": [],
        "fields": {},
        "locations": [],
        "omitted": True,
    }


def _serialize(frame: dict) -> str:
    payload = wire.dumps(frame)
    if len(payload.encode("utf-8")) > MAX_BYTES:
        raise ValueError("init diagnostic exceeds its closed frame limit")
    return payload


def diagnostic(scope: Scope, phase: str, error: BaseException) -> str:
    """Return bounded builtin DATA without exception display or local values."""
    if type(scope) is not Scope or type(phase) is not str or phase not in (
        "original-factory", "transport-registry",
    ):
        raise ValueError("invalid trusted init diagnostic scope or phase")
    # Identity comparisons also avoid unknown metaclass hashing/equality hooks.
    kind = next((tag for cls, tag in _KINDS if type(error) is cls),
                "unrepresented-exception")
    minimal = _minimal(scope, phase, kind)
    if kind == "unrepresented-exception":
        return _serialize(minimal)
    try:
        frame = _minimal(scope, phase, kind)
        frame["omitted"] = False
        args = BaseException.args.__get__(error, type(error))
        if type(args) is not tuple:
            return _serialize(minimal)
        for value in args[:4]:
            if not _scalar(value):
                frame["omitted"] = True
                break
            frame["args"].append(value)
        if len(args) > len(frame["args"]):
            frame["omitted"] = True
        for cls, name in _FIELDS.get(type(error), ()):
            value = vars(cls)[name].__get__(error, type(error))
            if _scalar(value):
                frame["fields"][name] = value
            else:
                frame["omitted"] = True
        trace = BaseException.__traceback__.__get__(error, type(error))
        for _ in range(4):
            if trace is None:
                break
            code = trace.tb_frame.f_code
            filename, function, line = code.co_filename, code.co_name, trace.tb_lineno
            if (_text(filename, 256) and _text(function, 64)
                    and type(line) is int and 1 <= line <= _SAFE_INTEGER):
                frame["locations"].append([filename, function, line])
            else:
                frame["omitted"] = True
            trace = trace.tb_next
        if trace is not None:
            frame["omitted"] = True
        return _serialize(frame)
    except BaseException:
        return _serialize(minimal)


def report_init_failure(scope: Scope, phase: str, error: BaseException) -> None:
    """Try the existing fixed import; its reply confers no success or grant."""
    from wit_world.imports.bindings import inspect

    inspect(diagnostic(scope, phase, error))
