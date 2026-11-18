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


def derive_catalog_validation(source: bytes) -> bytes:
    """Use the fixed reader's digest-checked document while preserving semantics."""
    from bundle_catalog_kernel_inputs import _replace_one

    source = _replace_one(source, b"import hashlib\n", b"import hashlib\nimport json\n")
    source = _replace_one(
        source, b"from .validation import validate_document\n",
        b"from .validation import (\n"
        b"    _semantic_diagnostics,\n"
        b"    _versioned_schema_name,\n"
        b"    validate_document,\n"
        b")\n",
    )
    source = _replace_one(
        source, b"        def read_native_entry(index: int, relative: str, reread: bool) -> bytes:\n",
        b"        from harness_native.native_path import NativePath\n\n"
        b"        native = type(root) is NativePath and root._bridge._catalog_root is not None\n\n"
        b"        def read_native_entry(index: int, relative: str, reread: bool) -> bytes | str:\n",
    )
    source = _replace_one(
        source, b"            return candidate.read_bytes()\n",
        b"            if native:\n"
        b"                return candidate._read_catalog_document()\n"
        b"            return candidate.read_bytes()\n",
    )
    source = _replace_one(
        source, b"        return cls.load_from_reader(\n",
        b'''        if native:
            return cls._load_from_reader(
                lock_bytes,
                entry_reader=read_native_entry,
                root=root,
                artifact_root_resolver=resolve_native_artifact_root,
                native_documents=True,
            )
        return cls.load_from_reader(
''',
    )
    source = _replace_one(
        source, b"        try:\n            lock = strict_loads(lock_bytes)\n",
        b'''        return cls._load_from_reader(
            lock_bytes,
            entry_reader=entry_reader,
            root=root,
            artifact_root_resolver=artifact_root_resolver,
        )

    @classmethod
    def _load_from_reader(
        cls,
        lock_bytes: bytes,
        *,
        entry_reader: Callable[[int, str, bool], bytes | str],
        root: Any,
        artifact_root_resolver: Callable[[], Any],
        native_documents: bool = False,
    ) -> "BundledCatalog":
        try:
            lock = strict_loads(lock_bytes)
''',
    )
    digest = b'''            actual_digest = 'sha256:' + hashlib.sha256(raw).hexdigest()
            if actual_digest != expected_digest:
                raise BundleIntegrityError(
                    f"bundle artifact digest changed: {relative} "
                    f"({actual_digest} != {expected_digest})"
                )
'''
    source = _replace_one(
        source, digest,
        b"            native_document = native_documents and type(raw) is str\n"
        b"            if not native_document:\n"
        + b"".join(b"    " + line for line in digest.splitlines(keepends=True)),
    )
    validation = b'''                document = (
                    copy.deepcopy(_validated_document(raw, kind))
                    if len(raw) <= _MAX_CACHED_DOCUMENT_BYTES
                    else copy.deepcopy(_validated_large_document(raw, kind))
                    if len(raw) <= _MAX_LARGE_DOCUMENT_BYTES
                    else validate_document(raw, kind)
                )
'''
    replacement = b'''                if native_document:
                    # The fixed read already checked the digest, strict JSON and schema.
                    document = json.loads(raw)
                    schema_name = _versioned_schema_name(document, kind)
                    diagnostics = _semantic_diagnostics(document, schema_name, True)
                    if diagnostics:
                        raise SchemaValidationError(
                            f"{schema_name} validation failed", diagnostics=tuple(diagnostics)
                        )
                else:
'''
    return _replace_one(source, validation, replacement + b"".join(
        b"    " + line for line in validation.splitlines(keepends=True)
    ))


def derive_harness_sources(files: dict[str, bytes], *, catalog_shapes: bool = False) -> dict[str, bytes]:
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
    changes = {validation: select_app_provider(files[validation])}
    if catalog_shapes:
        catalog = "tobkiri_protocol/bundle_catalog.py"
        changes[catalog] = derive_catalog_validation(files[catalog])
    runtime.update({"source-package/tobkiri_runtime/" + path: raw for path, raw in changes.items()})
    return {**changes, index: _source_index(runtime)[0]}


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
