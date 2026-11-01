"""CI-only connection to the already source-built private Rust kernel.

This authenticates the native precheck outputs before first library import.
It creates no validator, input, validation result, fallback or new optimization.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys

HERE = Path(__file__).resolve().parent


def install(precheck: Path, packages: list[Path]) -> tuple[object, list[Path], dict]:
    """Bind the same authenticated source translation and actual adapter."""
    from app_schema_oracle import physical, require_ci
    from build_native import fingerprint
    from fetch_sdk import inventory
    from schema_kernel_translate import translate

    require_ci()
    physical(precheck)
    receipt_path = precheck / "schema-kernel-native-check.json"
    physical(receipt_path)
    if receipt_path.stat().st_size > 4 * 1024 * 1024:
        raise ValueError("bounded native precheck receipt required")
    receipt = json.loads(receipt_path.read_bytes())
    if (receipt.get("format") != "tobkiri.schema-kernel-native-check/1"
            or receipt.get("outcome") != "success"
            or receipt.get("native_64_success") is not True
            or receipt.get("api_19_success") is not True
            or receipt.get("source_head") != os.environ.get("GITHUB_SHA")
            or receipt.get("production_enabled") is not False):
        raise ValueError("successful actual native kernel precheck required")
    source = HERE / "schema_kernel"
    if inventory(source) != receipt["source_files"]:
        raise ValueError("actual kernel source differs from the compiled precheck")
    extension = precheck / "module/_tobkiri_schema_kernel.abi3.so"
    physical(extension)
    if fingerprint(extension, 32 * 1024 * 1024) != receipt["extension"]:
        raise ValueError("actual source-built native extension differs")
    selected = [path for path in packages if path.name == "jsonschema"]
    if len(selected) != 1:
        raise ValueError("one verified original jsonschema package required")
    original = selected[0]
    overlay = precheck / "translated-jsonschema"
    physical(overlay)
    raw = (original / "jsonschema/_keywords.py").read_bytes()
    translated, translation = translate(raw)
    if translation != receipt["keyword_translation"]:
        raise ValueError("native precheck keyword translation differs")
    expected = {}
    for path in original.rglob("*"):
        physical(path)
        if path.is_file():
            expected[path.relative_to(original).as_posix()] = fingerprint(path)
    expected["jsonschema/_keywords.py"] = {
        "bytes": len(translated), "sha256": hashlib.sha256(translated).hexdigest()}
    expected["jsonschema/_tobkiri_original_keywords.py"] = {
        "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}
    actual = {}
    for path in overlay.rglob("*"):
        physical(path)
        if path.is_file():
            actual[path.relative_to(overlay).as_posix()] = fingerprint(path)
    if actual != expected:
        raise ValueError("derived library closure differs from the declared translation")
    if any(name == "jsonschema" or name.startswith("jsonschema.")
           or name == "_tobkiri_schema_kernel" for name in sys.modules):
        raise ValueError("kernel lane must precede first library/extension import")
    packages = [overlay if path == original else path for path in packages]
    sys.path[:0] = [str(precheck / "module"), *(str(path) for path in packages)]
    import _tobkiri_schema_kernel as kernel
    import jsonschema
    from jsonschema import _keywords

    if (Path(kernel.__file__).resolve(strict=True) != extension
            or kernel.production_enabled is not False
            or kernel.implemented_handler_count != 36
            or Path(jsonschema.__file__).resolve(strict=True)
            != overlay / "jsonschema/__init__.py"
            or Path(_keywords.__file__).resolve(strict=True)
            != overlay / "jsonschema/_keywords.py"
            or getattr(_keywords, "_tobkiri_translated_keywords", False) is not True):
        raise ValueError("actual native kernel/translated library selection differs")
    spec = importlib.util.spec_from_file_location("schema_adapter", source / "adapter.py")
    if spec is None or spec.loader is None or "schema_adapter" in sys.modules:
        raise ValueError("fresh actual adapter source required")
    adapter = importlib.util.module_from_spec(spec)
    sys.modules["schema_adapter"] = adapter
    spec.loader.exec_module(adapter)
    adapter.install_standard_classes()
    if (jsonschema.Draft202012Validator.iter_errors.__module__ != "schema_adapter"
            or jsonschema.Draft202012Validator.descend.__module__ != "schema_adapter"):
        raise ValueError("actual App-selected class did not bind the real adapter")
    if kernel.keyword_entry_count() != 0:
        raise ValueError("kernel executed schema checks before the actual App corpus")
    return kernel, packages, {
        "extension": receipt["extension"], "source_files": receipt["source_files"],
        "keyword_translation": translation,
        "native_64_success": True, "api_19_success": True,
        "precheck_receipt": fingerprint(receipt_path, 4 * 1024 * 1024),
    }
