"""Bind the existing CI-built App schema extension and its fixed source hooks."""

from hashlib import sha256
import json
import os
from pathlib import Path

from build_native import fingerprint
from fetch_sdk import inventory


SOURCE = Path(__file__).resolve().parent / "schema_kernel"
ARTIFACT = "work/target/wasm32-wasip2/release/_tobkiri_schema_kernel.wasm"
EXTENSION = "_tobkiri_schema_kernel.abi3.so"
MAX_FILE = 4 * 1024 * 1024
PROVIDER_FILES = {"app_schema_provider.py": "app_provider.py", "app_schema_model.py": "app_schema_model.py"}
APP_IMPORT = (b"    from app_schema_provider import AppSchemaProvider as Draft202012Validator\n"
              b"    from jsonschema import FormatChecker, RefResolver\n")


def verify_report(report: dict, features: list[str]) -> None:
    if (report.get("format") != "tobkiri.app-schema-kernel-extension/1"
            or report.get("outcome") != "success"
            or report.get("guest_executed") is not False
            or report.get("production_enabled") is not False
            or report.get("source_head") != os.environ.get("GITHUB_SHA")
            or not os.environ.get("GITHUB_SHA")
            or report.get("features") != features
            or set(report.get("compiled_features", ())) != {"default", *features}
            or report.get("source_files") != inventory(SOURCE)
            or report.get("artifact_path") != ARTIFACT):
        raise ValueError("actual source-built kernel derivation required")


def verify_extension(root: Path, features: list[str]) -> tuple[Path, dict]:
    if (os.environ.get("GITHUB_ACTIONS") != "true" or not root.is_absolute()
            or root.resolve(strict=True) != root):
        raise ValueError("physical CI extension build required")
    receipt = root / "extension-build.json"
    fingerprint(receipt, MAX_FILE)
    report = json.loads(receipt.read_bytes())
    verify_report(report, features)
    artifact = root / ARTIFACT
    if (artifact.resolve(strict=True) != artifact
            or fingerprint(artifact, MAX_FILE) != report["artifact"]):
        raise ValueError("actual Wasm kernel bytes differ")
    with artifact.open("rb") as stream:
        if stream.read(8) != b"\x00asm\x01\x00\x00\x00":
            raise ValueError("actual core Wasm kernel required")
    return artifact, report


def select_app_provider(source: bytes) -> bytes:
    anchor = b"    from jsonschema import Draft202012Validator, FormatChecker, RefResolver\n"
    if source.count(anchor) != 1 or b"app_schema_provider" in source:
        raise ValueError("exact original App default selection anchor required")
    return source.replace(anchor, APP_IMPORT)


def derive_harness_sources(files: dict[str, bytes]) -> dict[str, bytes]:
    """Rebuild the native source index using exactly the derived runtime bytes."""
    from harness_native.schema_diagnostic_stage import _index_values, _source_index

    validation = "tobkiri_protocol/validation.py"
    index = "harness_native_source_index.py"
    rows = _index_values(files[index])["MODULES"]
    runtime = {}
    for _module, path, digest in rows:
        raw = files[path]
        if sha256(raw).hexdigest() != digest:
            raise ValueError("authenticated Harness source index differs from staged source")
        runtime["source-package/tobkiri_runtime/" + path] = raw
    if _source_index(runtime)[0] != files[index]:
        raise ValueError("authenticated Harness source index differs")
    derived = select_app_provider(files[validation])
    runtime["source-package/tobkiri_runtime/" + validation] = derived
    return {validation: derived, index: _source_index(runtime)[0]}


APP_PRELOAD = b'''if any(name == "jsonschema" or name.startswith("jsonschema.")
       or name == "_tobkiri_schema_kernel" for name in sys.modules):
    raise RuntimeError("App backend must precede first library import")
_kernel = importlib.import_module("_tobkiri_schema_kernel")
if any(name == "jsonschema" or name.startswith("jsonschema.") for name in sys.modules):
    raise RuntimeError("kernel imported the library before explicit App selection")
if (not _kernel.__file__.endswith("/_tobkiri_schema_kernel.abi3.so")
        or _kernel.app_schema_entry_count() != 0 or _kernel.app_schema_frame_count() != 0):
    raise RuntimeError("App extension origin or preinit validation differs")
'''


def derive_harness_entry(source: bytes) -> bytes:
    anchor = b"import jsonschema  # noqa: E402, F401\n"
    if source.count(anchor) != 1 or b"_tobkiri_schema_kernel" in source:
        raise ValueError("exact whole-Harness schema preload anchor required")
    return source.replace(anchor, APP_PRELOAD + anchor)
