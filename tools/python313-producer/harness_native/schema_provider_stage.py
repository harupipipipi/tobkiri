"""Explicit CI-only source selection of the App-owned schema provider.

The derivation runs AFTER the unchanged whole Harness (or its separate schema
checkpoint derivation) authenticates. It changes exactly one original
statement: the import that binds the module-global ``Draft202012Validator``.
The original ``load_schema`` try/call/except, caches, reader, strict parser and
``_schema_store`` bytes stay unchanged. Nothing is validated, loaded or cached
here, and no result is prepared. A failed or refused provider call never starts
a second validator (the provider itself decides before validation begins).
Production adoption stays off: this is a separate explicitly selected artifact.
"""

from __future__ import annotations

import difflib
import hashlib
import json
from typing import Mapping

from .schema_diagnostic_stage import _index_values, _json
from .source_package import _source_index


FORMAT = "io.tobkiri.harness-native-app-schema-provider-source.v1"
BASELINE_FORMAT = "io.tobkiri.harness-native-component-source.v1"
DIAGNOSTIC_FORMAT = "io.tobkiri.harness-native-schema-call-diagnostic-source.v1"
COMPONENT = "harness-native-app-schema-provider"
DIAGNOSTIC_COMPONENT = "harness-native-app-schema-provider-diagnostic"
VALIDATION = "python/tobkiri_protocol/validation.py"
INDEX = "python/harness_native_source_index.py"
ORIGINAL_IMPORT = (
    b"    from jsonschema import Draft202012Validator, FormatChecker, RefResolver\n"
)
SELECTED_IMPORT = (
    b"    from app_schema_provider import AppSchemaProvider as Draft202012Validator\n"
    b"    from jsonschema import FormatChecker, RefResolver\n"
)
_VALIDATION_PREFIX = "source-package/tobkiri_runtime/"


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def component_name(*, diagnostic: bool) -> str:
    """Name the closed artifact; the baseline name is never reused."""
    if type(diagnostic) is not bool:
        raise TypeError("diagnostic selection must be an explicit boolean")
    return DIAGNOSTIC_COMPONENT if diagnostic else COMPONENT


def select(raw: bytes) -> bytes:
    """Replace only the dependency import; every other byte remains original."""
    if raw.count(ORIGINAL_IMPORT) != 1:
        raise ValueError("original validation import changed before provider derivation")
    if b"app_schema_provider" in raw:
        raise ValueError("validation was already derived")
    derived = raw.replace(ORIGINAL_IMPORT, SELECTED_IMPORT)
    if derived.replace(SELECTED_IMPORT, ORIGINAL_IMPORT) != raw:
        raise ValueError("provider derivation changed an original validation statement")
    return derived


def unselect(derived: bytes) -> bytes:
    """Independently restore the exact received bytes."""
    if derived.count(SELECTED_IMPORT) != 1:
        raise ValueError("provider validation import markers changed")
    return derived.replace(SELECTED_IMPORT, ORIGINAL_IMPORT)


def _runtime(files: Mapping[str, bytes], values: dict) -> dict[str, bytes]:
    runtime = {}
    for _module, path, _digest in values["MODULES"]:
        runtime[_VALIDATION_PREFIX + path] = files["python/" + path]
    return runtime


def derive(base: Mapping[str, bytes]) -> dict[str, bytes]:
    """Derive the explicit provider source from an authenticated baseline or
    schema-checkpoint source mapping; execute none of it."""
    if (not isinstance(base, Mapping)
            or any(type(path) is not str or type(raw) is not bytes
                   for path, raw in base.items())):
        raise TypeError("provider baseline must be immutable source bytes")
    manifest = json.loads(base["provenance.json"])
    base_format = manifest.get("format")
    diagnostic = base_format == DIAGNOSTIC_FORMAT
    if (base_format not in (BASELINE_FORMAT, DIAGNOSTIC_FORMAT)
            or manifest.get("production_enabled") is not False):
        raise ValueError("authenticated component source required")
    recorded = {row["path"]: {"bytes": row["bytes"], "sha256": row["sha256"]}
                for row in manifest["sources"]}
    actual = {path: {"bytes": len(raw), "sha256": _sha(raw)}
              for path, raw in base.items() if path != "provenance.json"}
    if recorded != actual:
        raise ValueError("authenticated component source changed")
    values = _index_values(base[INDEX])
    runtime = _runtime(base, values)
    index, checksum, modules, path_modules = _source_index(runtime)
    if index != base[INDEX]:
        raise ValueError("baseline source index is not its actual runtime bytes")
    original = base[VALIDATION]
    files = dict(base)
    files[VALIDATION] = select(original)
    runtime[_VALIDATION_PREFIX + "tobkiri_protocol/validation.py"] = files[VALIDATION]
    index, checksum, modules, path_modules = _source_index(runtime)
    files[INDEX] = index
    allowed = {VALIDATION, INDEX, "provenance.json"}
    if any(files[path] != raw for path, raw in base.items() if path not in allowed):
        raise ValueError("provider derivation changed another original member")
    inventory = [{"path": path, "bytes": len(raw), "sha256": _sha(raw)}
                 for path, raw in sorted(files.items()) if path != "provenance.json"]
    provenance = {
        "format": FORMAT, "component_name": component_name(diagnostic=diagnostic),
        "base_provenance_format": base_format,
        "base_provenance_sha256": _sha(base["provenance.json"]),
        "base_provenance": base["provenance.json"].decode(),
        "default_off": True, "explicit_ci_selection": True,
        "original_source_files_unchanged": False,
        "other_original_members_unchanged": True,
        "validation_derivation": {
            "path": VALIDATION, "original_sha256": _sha(original),
            "derived_sha256": _sha(files[VALIDATION]),
            "diff": "".join(difflib.unified_diff(
                original.decode().splitlines(keepends=True),
                files[VALIDATION].decode().splitlines(keepends=True),
                fromfile="original/tobkiri_protocol/validation.py",
                tofile="provider/tobkiri_protocol/validation.py")),
            "selected_global": "Draft202012Validator",
            "selected_provider": "app_schema_provider.AppSchemaProvider",
            "original_try_call_except_bytes_unchanged": True,
            "provider_selected_before_cache_use": True,
            "provider_substitution_after_failure": False,
            "validation_retry": False, "success_shortcut": False, "cache_preload": False,
        },
        "source_index": {
            "path": "source-package/harness_native_source_index.py", "sha256": _sha(index),
            "modules": modules, "path_modules": path_modules, "index_sha256": checksum,
        },
        "sources": inventory,
        "diagnostic_only": diagnostic,
        "external_dependencies_staged": False, "build_performed": False,
        "component_init_performed": False, "native_imports_connected": False,
        "whole_setup_compatible": False, "production_enabled": False,
    }
    if diagnostic:
        contract = manifest["checkpoint_contract"]
        provenance["checkpoint_contract"] = contract
    files["provenance.json"] = _json(provenance)
    return files


def unwrap(files: Mapping[str, bytes]) -> dict[str, bytes]:
    """Return the exact base mapping a derived provider source came from."""
    manifest = json.loads(files["provenance.json"])
    if manifest.get("format") != FORMAT:
        raise ValueError("explicit provider source provenance is required")
    base_provenance = manifest["base_provenance"].encode()
    if _sha(base_provenance) != manifest["base_provenance_sha256"]:
        raise ValueError("provider base provenance changed")
    base = dict(files)
    base[VALIDATION] = unselect(files[VALIDATION])
    values = _index_values(files[INDEX])
    runtime = _runtime(base, values)
    base[INDEX] = _source_index(runtime)[0]
    base["provenance.json"] = base_provenance
    return base


def validate(files: Mapping[str, bytes]) -> dict:
    """Recheck the exact derivation and every original/staged byte; execute none."""
    base = unwrap(files)
    if derive(base) != dict(files):
        raise ValueError("provider source/index/provenance differs from the declared derivation")
    return json.loads(files["provenance.json"])
