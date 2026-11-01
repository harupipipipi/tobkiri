"""Build-time translation of the pinned keyword definitions, never live modules."""

from __future__ import annotations

import ast
import hashlib
import os
from pathlib import Path
import shutil
import sys

ORIGINAL_SHA256 = "afcfc3aea01f9fa40bc109e65c4820bde89253e51a20bda9da7b8f20d7a57c57"
KEYWORDS = (
    "patternProperties propertyNames additionalProperties items const contains "
    "exclusiveMinimum exclusiveMaximum minimum maximum multipleOf minItems "
    "maxItems uniqueItems pattern format minLength maxLength dependentRequired "
    "dependentSchemas enum ref dynamicRef type properties required minProperties "
    "maxProperties allOf anyOf oneOf not_ if_ unevaluatedItems "
    "unevaluatedProperties prefixItems"
).split()


def translate(raw: bytes) -> tuple[bytes, dict]:
    """Replace only the 36 known generator bodies before their first import."""
    digest = hashlib.sha256(raw).hexdigest()
    if digest != ORIGINAL_SHA256:
        raise ValueError("pinned jsonschema 4.26.0 keyword source differs")
    tree = ast.parse(raw, filename="jsonschema/_keywords.py")
    functions = [node for node in tree.body if isinstance(node, ast.FunctionDef)]
    if [node.name for node in functions] != KEYWORDS:
        raise ValueError("closed keyword definition sequence differs")
    signatures = {}
    for node in functions:
        args = node.args
        names = [arg.arg for arg in args.args]
        if (len(names) != 4 or names[0] != "validator"
                or names[2:] != ["instance", "schema"]
                or args.posonlyargs or args.kwonlyargs or args.vararg or args.kwarg
                or args.defaults or args.kw_defaults or node.decorator_list
                or not any(isinstance(item, (ast.Yield, ast.YieldFrom))
                           for item in ast.walk(node))):
            raise ValueError("original keyword signature/generator shape differs")
        signatures[node.name] = names
        keyword = {"ref": "$ref", "dynamicRef": "$dynamicRef",
                   "not_": "not", "if_": "if"}.get(node.name, node.name)
        body = ast.parse(
            "yield from _tobkiri_keyword_kernel.keyword_iter("
            + repr(keyword) + ", " + ", ".join(names)
            + ", _tobkiri_keyword_names)"
        ).body
        doc = node.body[:1] if ast.get_docstring(node, clean=False) is not None else []
        node.body = doc + body
    # These are ordinary definitions in a newly built module, not runtime
    # mutation/reload of an existing module, function, code object or alias.
    setup = ast.parse(
        "import _tobkiri_schema_kernel as _tobkiri_keyword_kernel\n"
        "_tobkiri_keyword_names = _tobkiri_keyword_kernel.keyword_names()\n"
        "_tobkiri_translated_keywords = True\n"
    ).body
    first_function = next(index for index, node in enumerate(tree.body)
                          if isinstance(node, ast.FunctionDef))
    tree.body[first_function:first_function] = setup
    ast.fix_missing_locations(tree)
    translated = (ast.unparse(tree) + "\n").encode()
    ast.parse(translated)
    return translated, {
        "format": "tobkiri.schema-keyword-source-translation/1",
        "original_sha256": digest,
        "translated_sha256": hashlib.sha256(translated).hexdigest(),
        "original_bytes": len(raw), "translated_bytes": len(translated),
        "signatures": signatures, "generator_functions": len(functions),
        "runtime_module_replacement": False,
        "validation_results_prepared": False,
    }


def require_ci() -> None:
    """Filesystem staging is only permitted in supervised Linux CI."""
    if sys.platform != "linux" or os.environ.get("GITHUB_ACTIONS") != "true":
        raise RuntimeError("keyword translation staging requires Linux CI")


def native_overlay(original: Path, destination: Path) -> dict:
    """Keep the authenticated original package untouched for the oracle."""
    require_ci()
    if any(name == "jsonschema" or name.startswith("jsonschema.")
           for name in sys.modules):
        raise ValueError("keyword translation must precede first jsonschema import")
    raw = (original / "jsonschema/_keywords.py").read_bytes()
    translated, receipt = translate(raw)
    shutil.copytree(original, destination)
    (destination / "jsonschema/_keywords.py").write_bytes(translated)
    oracle = destination / "jsonschema/_tobkiri_original_keywords.py"
    if oracle.exists():
        raise ValueError("independent keyword oracle namespace collision")
    oracle.write_bytes(raw)
    if (original / "jsonschema/_keywords.py").read_bytes() != raw:
        raise ValueError("original keyword source changed during translation")
    return receipt


def captured_overlay(stage: Path) -> dict:
    """Derive exactly one captured source member and its immutable data index."""
    require_ci()
    index = stage / "wheel_data.py"
    tree = ast.parse(index.read_bytes())
    values = {}
    for node in tree.body:
        if (not isinstance(node, ast.Assign) or len(node.targets) != 1
                or not isinstance(node.targets[0], ast.Name)):
            raise ValueError("captured data index is not a closed literal module")
        name = node.targets[0].id
        if name in values or name not in {"FILES", "DISTRIBUTIONS"}:
            raise ValueError("captured data index assignments differ")
        values[name] = ast.literal_eval(node.value)
    if set(values) != {"FILES", "DISTRIBUTIONS"}:
        raise ValueError("complete captured data index required")
    files = values["FILES"]
    member = "jsonschema/_keywords.py"
    raw = files[member]
    target = stage / member
    if type(raw) is not bytes or target.read_bytes() != raw:
        raise ValueError("captured source and authenticated data member differ")
    translated, receipt = translate(raw)
    oracle_member = "jsonschema/_tobkiri_original_keywords.py"
    oracle = stage / oracle_member
    if oracle_member in files or oracle.exists():
        raise ValueError("independent keyword oracle namespace collision")
    files[oracle_member] = raw
    oracle.write_bytes(raw)
    files[member] = translated
    target.write_bytes(translated)
    index.write_text(
        "# Authenticated originals with the declared build-time keyword translation.\n"
        "FILES = " + repr(files) + "\nDISTRIBUTIONS = "
        + repr(values["DISTRIBUTIONS"]) + "\n"
    )
    receipt["derived_members"] = [member, oracle_member, "wheel_data.py"]
    return receipt
