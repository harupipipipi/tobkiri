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
    """Decode the document proved by the fixed native read."""
    from bundle_catalog_kernel_inputs import _replace_one

    source = _replace_one(source, b"import hashlib\n", b"import hashlib\nimport json\n")
    source = _replace_one(
        source, b"        requested_root = Path(root)\n",
        b'''        from harness_native.native_path import NativePath

        if (
            artifact_root is None
            and type(root) is NativePath
            and root is root._bridge._catalog_snapshot_root
        ):
            return cls._load_native_snapshot(root)
        requested_root = Path(root)
''',
    )
    source = _replace_one(
        source, b"        def read_native_entry(index: int, relative: str, reread: bool) -> bytes:\n",
        b"        native = type(root) is NativePath and root._bridge._catalog_root is not None\n\n"
        b'''        def read_native_entry(index: int, relative: str, reread: bool) -> bytes | str:
            if native:
                path = root / relative
                if reread:
                    return path._read_catalog_pin_digest(index)
                kind, value = path._read_catalog_entry(index)
                if kind == "catalog-path-rejection":
                    if value == "null-byte":
                        raise ValueError("lstat: embedded null character in path")
                    if value == "escape":
                        raise BundleIntegrityError(
                            f"bundle entry escapes root: {relative}"
                        )
                    raise BundleIntegrityError(
                        f"bundle entry contains a symlink: {relative}"
                    )
                return value
''',
    )
    source = _replace_one(
        source, b"        return cls.load_from_reader(\n",
        b'''        if native:
            return cls._load_from_reader(
                lock_bytes,
                entry_reader=read_native_entry,
                root=root,
                artifact_root_resolver=resolve_native_artifact_root,
                native_reads=True,
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
        native_reads: bool = False,
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
        b"            native_document = native_reads and type(raw) is str\n"
        b"            if not native_document:\n"
        b"                native_identity_prefix = False\n"
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
                    # The fixed read also proved the Base and Shell definition revisions.
                    document = json.loads(raw)
                else:
'''
    source = _replace_one(source, validation, replacement + b"".join(
        b"    " + line for line in validation.splitlines(keepends=True)
    ))
    source = _replace_one(
        source,
        b'            if kind in {"base", "shell"}:\n',
        b'            if not native_document and kind in {"base", "shell"}:\n',
    )
    source = _replace_one(
        source,
        b"        for index, entry in enumerate(entries):\n",
        b"        native_identity_prefix = native_reads\n"
        b"        for index, entry in enumerate(entries):\n",
    )
    source = _replace_one(
        source,
        b"            if not isinstance(identity, str) or identity in collections[kind]:\n",
        b'''            # A raw entry ends native uniqueness proof for the rest of the load.
            if not isinstance(identity, str) or (
                not native_identity_prefix and identity in collections[kind]
            ):
''',
    )
    source = _derive_catalog_snapshot(source)
    return _replace_one(
        source,
        b"            catalog_raw_digest = 'sha256:' + hashlib.sha256(catalog_raw).hexdigest()\n",
        b'''            catalog_raw_digest = (
                catalog_raw
                if native_reads
                else 'sha256:' + hashlib.sha256(catalog_raw).hexdigest()
            )
''',
    )


def _derive_catalog_snapshot(source: bytes) -> bytes:
    from bundle_catalog_kernel_inputs import _replace_one

    anchor = b"    @classmethod\n    def load_from_reader(\n"
    snapshot = b'''    @classmethod
    def _load_native_snapshot(cls, root: Any) -> "BundledCatalog":
        from harness_native.value_graph import UnsupportedValue
        from .errors import CanonicalizationError

        opened = root._catalog_snapshot("snapshot-open", [])
        if opened.get("kind") == "snapshot-error":
            error = opened.get("error")
            message = opened.get("message")
            fields = {"kind", "error", "message"}
            if error in ("lock-io", "lock-json"):
                fields.add("cause")
            if set(opened) != fields or type(message) is not str:
                raise UnsupportedValue("invalid native catalog snapshot error")
            if error == "integrity":
                raise BundleIntegrityError(message)
            if error == "type-error":
                raise TypeError(message)
            if error == "value-error":
                raise ValueError(message)
            if error == "lock-io" and type(opened["cause"]) is dict:
                cause = root._bridge._catalog_error(opened["cause"])
                if not isinstance(cause, OSError):
                    raise UnsupportedValue("invalid native catalog snapshot error")
            elif error == "lock-json" and type(opened["cause"]) is str:
                cause = CanonicalizationError(opened["cause"])
            else:
                raise UnsupportedValue("invalid native catalog snapshot error")
            try:
                raise cause
            except (OSError, CanonicalizationError) as caught:
                raise BundleIntegrityError(message) from caught

        if (
            set(opened) != {"kind", "root", "artifact_root", "count"}
            or opened["kind"] != "snapshot-open"
            or type(opened["root"]) is not str
            or (opened["artifact_root"] is not None and type(opened["artifact_root"]) is not str)
            or type(opened["count"]) is not int
            or opened["count"] < 1
        ):
            raise UnsupportedValue("invalid native catalog snapshot")
        resolved = root._bridge.bind(root._binding, opened["root"])
        artifact = None
        if opened["artifact_root"] is not None:
            artifact = root._bridge.bind(root._binding, opened["artifact_root"])
            artifact._steps = (("parent", ""), ("join", "platform-artifacts"))
        if not resolved.is_absolute() or (artifact is not None and not artifact.is_absolute()):
            raise UnsupportedValue("invalid native catalog snapshot path")

        identity_fields = {
            "pack": "id", "base": "pack_id", "shell": "provider_id",
            "profile": "profile_id", "executable_catalog": "pack_id",
        }
        collections = {kind: {} for kind in identity_fields}
        ordinal = 0
        count = opened["count"]
        while ordinal < count:
            chunk = root._catalog_snapshot("snapshot-rows", [ordinal])
            if (
                set(chunk) != {"kind", "rows"}
                or chunk["kind"] != "snapshot-rows"
                or type(chunk["rows"]) is not list
                or not 0 < len(chunk["rows"]) <= count - ordinal
            ):
                raise UnsupportedValue("invalid native catalog snapshot chunk")
            for row in chunk["rows"]:
                if (
                    type(row) is not list
                    or len(row) != 3
                    or any(type(value) is not str for value in row)
                ):
                    raise UnsupportedValue("invalid native catalog snapshot row")
                kind, identity, text = row
                if kind not in collections or identity in collections[kind]:
                    raise UnsupportedValue("invalid native catalog snapshot identity")
                try:
                    document = json.loads(text)
                except json.JSONDecodeError:
                    raise UnsupportedValue("invalid native catalog snapshot document") from None
                if type(document) is not dict:
                    raise UnsupportedValue("invalid native catalog snapshot document")
                owner = document.get("pack") if kind == "pack" else document
                if type(owner) is not dict or owner.get(identity_fields[kind]) != identity:
                    raise UnsupportedValue("native catalog snapshot identity differs")
                collections[kind][identity] = document
            ordinal += len(chunk["rows"])
            del chunk, row, text

        catalog = cls(
            root=resolved,
            packs=collections["pack"],
            bases=collections["base"],
            shells=collections["shell"],
            profiles=collections["profile"],
            artifact_root=artifact,
            executable_catalogs=collections["executable_catalog"],
        )
        root._finish_catalog_snapshot(count, catalog)
        return catalog

'''
    return _replace_one(source, anchor, snapshot + anchor)


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
