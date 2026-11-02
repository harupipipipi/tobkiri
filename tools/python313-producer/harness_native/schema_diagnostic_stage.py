"""Explicit CI-only derivation AFTER the unchanged whole Harness authenticates.

This does not assemble/authenticate an original capsule or admit runtime IO.
Only validation callsite checkpoints, their helper and the actual source index
are changed. Baseline source/provenance remain a separate, immutable proof.
"""

from __future__ import annotations

import ast
import difflib
import hashlib
import json
from pathlib import Path
from typing import Mapping

from .source_package import _source_index


FORMAT = "io.tobkiri.harness-native-schema-call-diagnostic-source.v1"
BASELINE_FORMAT = "io.tobkiri.harness-native-component-source.v1"
COMPONENT = "harness-native-schema-diagnostic"
VALIDATION = "python/tobkiri_protocol/validation.py"
HELPER = "python/harness_native/schema_checkpoint.py"
INDEX = "python/harness_native_source_index.py"
FRAME_FORMAT = "io.tobkiri.native-schema-checkpoint.v1"
_IMPORT = b"from harness_native.schema_checkpoint import checkpoint as _schema_checkpoint\n"
_IMPORT_ANCHOR = b"from functools import lru_cache\n"
_CHECK_ANCHOR = (
    b"    try:\n"
    b"        Draft202012Validator.check_schema(payload)\n"
    b"    except Exception as exc:  # jsonschema exposes several schema error classes.\n"
    b'        raise SchemaValidationError(f"invalid protocol schema {path}: {exc}") from exc\n'
)
_STORE_ANCHOR = b'    """Return absolute-ID schemas used by local JSON Schema references."""\n'
_TAGS_ANCHOR = b"_SCHEMA_TAGS = ()  # Filled only by the explicit diagnostic source derivation.\n"


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _json(value) -> bytes:
    return (json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False) + "\n").encode()


def _index_values(raw: bytes) -> dict:
    tree = ast.parse(raw)
    if any(not isinstance(node, ast.Assign) or len(node.targets) != 1
           or not isinstance(node.targets[0], ast.Name) for node in tree.body):
        raise ValueError("diagnostic baseline index is not pure DATA")
    values = {node.targets[0].id: ast.literal_eval(node.value) for node in tree.body}
    if len(tree.body) != 3 or set(values) != {"MODULES", "PATH_MODULES", "INDEX_SHA256"}:
        raise ValueError("diagnostic baseline index fields differ")
    return values


def instrument(raw: bytes) -> bytes:
    """Insert fixed markers without replacing the original try/call/except."""
    for anchor in (_IMPORT_ANCHOR, _CHECK_ANCHOR, _STORE_ANCHOR):
        if raw.count(anchor) != 1:
            raise ValueError("original validation callsite changed before diagnostic derivation")
    if b"_schema_checkpoint" in raw or b"harness_native.schema_checkpoint" in raw:
        raise ValueError("validation was already derived")
    derived = raw.replace(_IMPORT_ANCHOR, _IMPORT_ANCHOR + _IMPORT)
    derived = derived.replace(
        _CHECK_ANCHOR,
        b'    _schema_checkpoint("check-schema-start", schema_name)\n'
        + _CHECK_ANCHOR + b'    _schema_checkpoint("check-schema-end", schema_name)\n',
    )
    derived = derived.replace(
        _STORE_ANCHOR, _STORE_ANCHOR + b'    _schema_checkpoint("schema-store-entry")\n',
    )
    # Independently strip exactly those additions and compare complete bytes.
    restored = derived.replace(_IMPORT, b"").replace(
        b'    _schema_checkpoint("check-schema-start", schema_name)\n', b"",
    ).replace(b'    _schema_checkpoint("check-schema-end", schema_name)\n', b"").replace(
        b'    _schema_checkpoint("schema-store-entry")\n', b"",
    )
    if restored != raw:
        raise ValueError("diagnostic derivation changed an original validation statement")
    ast.parse(derived)
    return derived


def _tags(files: Mapping[str, bytes], original: bytes) -> tuple[tuple[str, str], ...]:
    tree = ast.parse(original)
    aliases = [node for node in tree.body if isinstance(node, ast.Assign)
               and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name)
               and node.targets[0].id == "SCHEMA_ALIASES"]
    if len(aliases) != 1:
        raise ValueError("one actual schema alias DATA table is required")
    aliases = ast.literal_eval(aliases[0].value)
    prefix = "python/tobkiri_protocol/schemas/"
    filenames = sorted(path.removeprefix(prefix) for path in files
                       if path.startswith(prefix) and path.endswith(".schema.json"))
    if (type(aliases) is not dict or len(filenames) != 42
            or any(type(key) is not str or type(value) is not str
                   or value not in filenames for key, value in aliases.items())
            or any("/" in name or "\\" in name for name in filenames)
            or set(aliases) & set(filenames)):
        raise ValueError("actual closed schema aliases/resources differ")
    tags = tuple(("alias", name) for name in sorted(aliases)) + tuple(
        ("filename", name) for name in filenames)
    if not 0 < len(tags) <= 128:
        raise ValueError("schema tag inventory exceeds its fixed bound")
    return tags


def derive(baseline: Mapping[str, bytes]) -> dict[str, bytes]:
    """Derive only the authenticated component-source mapping, never original IO."""
    if (not isinstance(baseline, Mapping)
            or any(type(path) is not str or type(raw) is not bytes
                   for path, raw in baseline.items())):
        raise TypeError("diagnostic baseline must be immutable source bytes")
    manifest = json.loads(baseline["provenance.json"])
    records = manifest["sources"]
    recorded = {row["path"]: {"bytes": row["bytes"], "sha256": row["sha256"]}
                for row in records}
    actual = {path: {"bytes": len(raw), "sha256": _sha(raw)}
              for path, raw in baseline.items() if path != "provenance.json"}
    if (manifest.get("format") != BASELINE_FORMAT or manifest.get("production_enabled") is not False
            or len(recorded) != len(records) or recorded != actual or HELPER in baseline):
        raise ValueError("authenticated baseline component source changed")
    values = _index_values(baseline[INDEX])
    runtime = {}
    for module, path, digest in values["MODULES"]:
        raw = baseline["python/" + path]
        if _sha(raw) != digest:
            raise ValueError("baseline runtime source/index mismatch")
        runtime["source-package/tobkiri_runtime/" + path] = raw
    index, checksum, modules, path_modules = _source_index(runtime)
    if (index != baseline[INDEX] or manifest["source_index"] != {
            "path": "source-package/harness_native_source_index.py",
            "sha256": _sha(index), "modules": modules,
            "path_modules": path_modules, "index_sha256": checksum}):
        raise ValueError("baseline source index is not its actual runtime bytes")
    original = baseline[VALIDATION]
    tags = _tags(baseline, original)
    helper_path = Path(__file__).with_name("schema_checkpoint.py")
    if helper_path.is_symlink():
        raise ValueError("physical diagnostic helper source required")
    helper_template = helper_path.read_bytes()
    if len(helper_template) > 64 * 1024 or helper_template.count(_TAGS_ANCHOR) != 1:
        raise ValueError("fixed diagnostic helper source changed")
    helper = helper_template.replace(_TAGS_ANCHOR, b"_SCHEMA_TAGS = " + repr(tags).encode() + b"\n")
    ast.parse(helper)
    files = dict(baseline)
    files[VALIDATION] = instrument(original)
    files[HELPER] = helper
    runtime["source-package/tobkiri_runtime/tobkiri_protocol/validation.py"] = files[VALIDATION]
    index, checksum, modules, path_modules = _source_index(runtime)
    files[INDEX] = index
    allowed = {VALIDATION, INDEX, "provenance.json"}
    if any(files[path] != raw for path, raw in baseline.items() if path not in allowed):
        raise ValueError("diagnostic derivation changed another original member")
    inventory = [{"path": path, "bytes": len(raw), "sha256": _sha(raw)}
                 for path, raw in sorted(files.items()) if path != "provenance.json"]
    files["provenance.json"] = _json({
        "format": FORMAT, "component_name": COMPONENT, "diagnostic_only": True,
        "baseline_component_source_sha256": _sha(baseline["provenance.json"]),
        "baseline_component_provenance": baseline["provenance.json"].decode(),
        "baseline_source_capsule_sha256": manifest["source_capsule_sha256"],
        "baseline_original_source_package_sha256": manifest["original_source_package_sha256"],
        "original_source_files_unchanged": False, "other_original_members_unchanged": True,
        "validation_derivation": {
            "path": VALIDATION, "original_sha256": _sha(original),
            "derived_sha256": _sha(files[VALIDATION]),
            "diff": "".join(difflib.unified_diff(
                original.decode().splitlines(keepends=True),
                files[VALIDATION].decode().splitlines(keepends=True),
                fromfile="original/tobkiri_protocol/validation.py",
                tofile="diagnostic/tobkiri_protocol/validation.py")),
            "original_try_call_except_bytes_unchanged": True,
            "provider_substitution": False, "cache_preload": False,
        },
        "helper": {"path": HELPER, "template_sha256": _sha(helper_template), "sha256": _sha(helper)},
        "checkpoint_contract": {
            "format": FRAME_FORMAT, "maximum_sequence": 128,
            "stages": ["check-schema-start", "check-schema-end", "schema-store-entry"],
            "schema_tags": [{"tag": tag, "kind": kind, "name": name}
                            for tag, (kind, name) in enumerate(tags)],
            "tag_count": len(tags), "guest_fuel_claimed": False,
            "normal_end_only": True, "measurement_errors_ignored": False,
        },
        "source_index": {
            "path": "source-package/harness_native_source_index.py", "sha256": _sha(index),
            "modules": modules, "path_modules": path_modules, "index_sha256": checksum,
        },
        "sources": inventory,
        "baseline_catalog_host_dependency_replacement": manifest["catalog_host_dependency_replacement"],
        "derived_closure_original_ast_guard_claimed": False,
        "external_runtime_dependencies": manifest["external_runtime_dependencies"],
        "external_dependencies_staged": False, "build_performed": False,
        "component_init_performed": False, "native_imports_connected": False,
        "whole_setup_compatible": False, "production_enabled": False,
    })
    return files


def validate(files: Mapping[str, bytes]) -> dict:
    """Recheck the exact derivation and every original/staged byte; execute none."""
    manifest = json.loads(files["provenance.json"])
    if manifest.get("format") != FORMAT:
        raise ValueError("explicit diagnostic source provenance is required")
    baseline_manifest = manifest["baseline_component_provenance"].encode()
    if _sha(baseline_manifest) != manifest["baseline_component_source_sha256"]:
        raise ValueError("diagnostic baseline provenance changed")
    baseline = dict(files)
    derived = baseline[VALIDATION]
    original = derived
    for addition in (
        _IMPORT,
        b'    _schema_checkpoint("check-schema-start", schema_name)\n',
        b'    _schema_checkpoint("check-schema-end", schema_name)\n',
        b'    _schema_checkpoint("schema-store-entry")\n',
    ):
        if original.count(addition) != 1:
            raise ValueError("diagnostic callsite markers changed")
        original = original.replace(addition, b"")
    if instrument(original) != derived:
        raise ValueError("diagnostic original call/except changed")
    baseline[VALIDATION] = original
    values = _index_values(baseline[INDEX])
    runtime = {"source-package/tobkiri_runtime/" + path: baseline["python/" + path]
               for _module, path, _digest in values["MODULES"]}
    baseline[INDEX] = _source_index(runtime)[0]
    baseline.pop(HELPER)
    baseline["provenance.json"] = baseline_manifest
    if derive(baseline) != dict(files):
        raise ValueError("diagnostic source/index/provenance differs from the declared derivation")
    return manifest
