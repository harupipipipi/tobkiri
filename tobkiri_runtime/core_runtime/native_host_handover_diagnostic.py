"""Bounded public-source support codes, without exception or user data."""

from __future__ import annotations

from pathlib import Path
import sys
from types import CodeType, FunctionType, ModuleType
from typing import Iterator

_ROOT = Path(__file__).absolute().parent.parent
_MODULES = {
    "ecosystem.defaultspack.defaultspack.development_host_handover": "host",
    "ecosystem.defaultspack.defaultspack.development_host_data": "data",
    "ecosystem.defaultspack.backend.sandbox.isolation.macos_vz_handover": "vm",
    "ecosystem.defaultspack.backend.sandbox.isolation.macos_vz_provisioner": "vz",
    "core_runtime.development_host_handover_guard": "guard",
}
_ACTIONS = frozenset({"prepare", "commit", "recovery-preview", "recover"})
_CATEGORIES = (
    (OSError, "os"),
    (ValueError, "value"),
    (RuntimeError, "runtime"),
    (KeyError, "key"),
    (TypeError, "type"),
)


def _nested_codes(code: CodeType, filename: str) -> Iterator[CodeType]:
    if code.co_filename != filename:
        return
    yield code
    for constant in code.co_consts:
        if type(constant) is CodeType:
            yield from _nested_codes(constant, filename)


def _owned_codes(module: ModuleType, name: str, filename: str) -> Iterator[CodeType]:
    for value in vars(module).values():
        if type(value) is FunctionType and value.__module__ == name:
            yield from _nested_codes(value.__code__, filename)
        elif type(value) is type and value.__module__ == name:
            # Do not evaluate properties or other arbitrary descriptors.
            for member in vars(value).values():
                if type(member) in (staticmethod, classmethod):
                    member = member.__func__
                if type(member) is FunctionType and member.__module__ == name:
                    yield from _nested_codes(member.__code__, filename)


def _source_origins(name: str) -> set[str]:
    if name.startswith("core_runtime."):
        return {str(_ROOT.joinpath(*name.split(".")).with_suffix(".py"))}
    # The native desktop bootstrap binds this canonical namespace to the
    # verified Pack root, which may be a sealed stage outside the Core checkout.
    package = sys.modules.get("ecosystem.defaultspack")
    if type(package) is not ModuleType:
        return set()
    relative = name.removeprefix("ecosystem.defaultspack.").split(".")
    return {
        str(Path(root).joinpath(*relative).with_suffix(".py"))
        for root in vars(package).get("__path__", ())
        if type(root) is str and Path(root).is_absolute()
    }


def _source_location(error: BaseException) -> str:
    trusted: dict[int, tuple[CodeType, str]] = {}
    for name, label in _MODULES.items():
        module = sys.modules.get(name)
        if type(module) is not ModuleType:
            continue
        filename = vars(module).get("__file__")
        if type(filename) is not str or filename not in _source_origins(name):
            continue
        for code in _owned_codes(module, name, filename):
            if code.co_name.isidentifier() and len(code.co_name) <= 80:
                trusted[id(code)] = (code, label)

    location = "unknown"
    traceback = BaseException.__traceback__.__get__(error)
    while traceback is not None:
        code = traceback.tb_frame.f_code
        entry = trusted.get(id(code))
        line = traceback.tb_lineno
        if (
            entry is not None
            and entry[0] is code
            and type(line) is int
            and 1 <= line <= 1_000_000
            and any(source_line == line for _, _, source_line in code.co_lines())
        ):
            location = f"{entry[1]}.{code.co_name}.{line}"
        traceback = traceback.tb_next
    return location


def handover_support_code(action: str, error: BaseException) -> str:
    """Identify only a trusted public code location and fixed error category.

    Never inspect exception text, arguments, chained exceptions, frame locals,
    source contents or user files. Unknown origins retain a constant fallback.
    Filename matching alone is insufficient: the frame must use the exact code
    object of a function owned by an already-loaded, allowlisted source module.
    """
    public_action = action if action in _ACTIONS else "unknown"
    category = next(
        (label for kind, label in _CATEGORIES if issubclass(type(error), kind)), "other"
    )
    try:
        location = _source_location(error)
    except Exception:
        # Diagnostics must never replace the original retained-data response.
        location = "unknown"
    return f"NH1.{public_action}.{category}.{location}"
