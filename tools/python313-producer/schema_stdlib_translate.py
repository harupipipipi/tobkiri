"""Translate one authenticated stdlib definition before its first import."""

from __future__ import annotations

import ast
import copy
import hashlib
from pathlib import Path
import shutil

from schema_kernel_translate import require_ci

ORIGINAL_SHA256 = "491b233e8d87a2ab73964afff029717177d53ad8def328ed9b00575db983da27"


def translate(raw: bytes) -> tuple[bytes, dict]:
    """Keep the original helper as oracle and bind a native callable on import."""
    if hashlib.sha256(raw).hexdigest() != ORIGINAL_SHA256:
        raise ValueError("pinned CPython 3.13.16 urllib.parse source differs")
    tree = ast.parse(raw, filename="urllib/parse.py")
    selected = [(i, node) for i, node in enumerate(tree.body)
                if isinstance(node, ast.FunctionDef) and node.name == "_coerce_args"]
    if len(selected) != 1:
        raise ValueError("one original argument coercion definition required")
    index, function = selected[0]
    args = function.args
    if (args.args or args.posonlyargs or args.kwonlyargs or args.kwarg
            or args.defaults or args.kw_defaults or args.vararg is None
            or args.vararg.arg != "args" or function.decorator_list
            or any(isinstance(node, (ast.Yield, ast.YieldFrom))
                   for node in ast.walk(function))):
        raise ValueError("original argument coercion signature differs")
    raises = [node for node in ast.walk(function) if isinstance(node, ast.Raise)]
    if len(raises) != 1:
        raise ValueError("one original mixed-type raise statement required")
    setup = ast.parse(
        "import _tobkiri_schema_kernel as _tobkiri_coerce_kernel\n"
        "def _tobkiri_raise_coerce_mixed():\n"
        "    pass\n"
        "_tobkiri_original_coerce_args = _coerce_args\n"
        "_coerce_args = _tobkiri_coerce_kernel.make_coerce_args(\n"
        "    _coerce_args, globals(), _coerce_args.__builtins__)\n"
        "_tobkiri_translated_coerce_args = True\n"
    ).body
    setup[1].body = [copy.deepcopy(raises[0])]
    # The unchanged original function retains its real name, globals and
    # builtins. This is construction of a new module, not a live replacement.
    tree.body[index + 1:index + 1] = setup
    ast.fix_missing_locations(tree)
    translated = (ast.unparse(tree) + "\n").encode()
    ast.parse(translated)
    return translated, {
        "format": "tobkiri.stdlib-coerce-source-translation/1",
        "original_sha256": ORIGINAL_SHA256,
        "original_bytes": len(raw),
        "translated_sha256": hashlib.sha256(translated).hexdigest(),
        "translated_bytes": len(translated),
        "definition": "urllib.parse._coerce_args(*args)",
        "original_helper_body_preserved": True,
        "runtime_module_replacement": False,
        "validation_results_prepared": False,
    }


def native_overlay(original: Path, destination: Path) -> dict:
    """Stage a fresh stdlib package; the parent never imports this overlay."""
    require_ci()
    raw = (original / "urllib/parse.py").read_bytes()
    translated, receipt = translate(raw)
    destination.mkdir(mode=0o700)
    shutil.copytree(original / "urllib", destination / "urllib",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"))
    (destination / "urllib/parse.py").write_bytes(translated)
    if (original / "urllib/parse.py").read_bytes() != raw:
        raise ValueError("original interpreter stdlib source changed")
    return receipt


def captured_overlay(stage: Path) -> dict:
    """Derive the declared source and data member within the original bound."""
    require_ci()
    index = stage / "wheel_data.py"
    values = {}
    for node in ast.parse(index.read_bytes()).body:
        if (not isinstance(node, ast.Assign) or len(node.targets) != 1
                or not isinstance(node.targets[0], ast.Name)):
            raise ValueError("captured stdlib data index is not a literal module")
        name = node.targets[0].id
        if name in values or name not in {"FILES", "DISTRIBUTIONS"}:
            raise ValueError("captured stdlib data assignments differ")
        values[name] = ast.literal_eval(node.value)
    if set(values) != {"FILES", "DISTRIBUTIONS"}:
        raise ValueError("complete captured stdlib index required")
    files = values["FILES"]
    member = "urllib/parse.py"
    target = stage / member
    raw = files[member]
    if type(raw) is not bytes or target.read_bytes() != raw:
        raise ValueError("original stdlib member and source differ")
    translated, receipt = translate(raw)
    files[member] = translated
    target.write_bytes(translated)
    index.write_text(
        "# Authenticated inputs with declared build-time source translations.\n"
        "FILES = " + repr(files) + "\nDISTRIBUTIONS = "
        + repr(values["DISTRIBUTIONS"]) + "\n"
    )
    receipt["derived_members"] = [member, "wheel_data.py"]
    return receipt
