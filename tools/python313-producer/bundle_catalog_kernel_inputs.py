"""A closed, explicitly separate derivation of the actual bundle-catalog stage."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from build_native import HERE, fingerprint
from fetch_sdk import inventory
from schema_kernel_translate import translate

COMPONENT = 'harness-bundle-catalog-rust-kernel-native'
APP_COMPONENT = 'harness-bundle-catalog-app-frame-native'
PROGRESS_COMPONENT = 'harness-app-schema-progress-native'


def component_name(*, app_backend: bool, schema_progress: bool = False) -> str:
    """Select a closed, distinct diagnostic artifact without changing defaults."""
    if (type(app_backend) is not bool or type(schema_progress) is not bool
            or (schema_progress and not app_backend)):
        raise ValueError('schema progress requires the explicit App backend')
    return PROGRESS_COMPONENT if schema_progress else APP_COMPONENT if app_backend else COMPONENT


def derive(files: dict[str, bytes], entry: bytes, export: bytes,
           extension_root: Path, *, app_backend: bool = False,
           schema_progress: bool = False) -> tuple[bytes, bytes, bytes, dict]:
    """Add only the actual kernel/adapter and declared pre-import translation."""
    if (type(app_backend) is not bool
            or os.environ.get('GITHUB_ACTIONS') != 'true'
            or not extension_root.is_absolute()
            or extension_root.resolve(strict=True) != extension_root):
        raise ValueError('physical CI extension build required')
    component_name(app_backend=app_backend, schema_progress=schema_progress)
    report_path = extension_root/'extension-build.json'
    report = json.loads(report_path.read_bytes())
    source = HERE/'schema_kernel'
    if (report.get('format') != 'tobkiri.app-schema-kernel-extension/1'
            or report.get('outcome') != 'success'
            or report.get('guest_executed') is not False
            or report.get('production_enabled') is not False
            or report.get('source_head') != os.environ.get('GITHUB_SHA')
            or report.get('features') != (['app-schema-backend'] if app_backend else [])
            or report.get('source_files') != inventory(source)
            or report.get('artifact_path')
            != 'work/target/wasm32-wasip2/release/_tobkiri_schema_kernel.wasm'):
        raise ValueError('actual source-built kernel derivation required')
    artifact = extension_root/report['artifact_path']
    if (artifact.resolve(strict=True) != artifact
            or fingerprint(artifact, 4*1024*1024) != report['artifact']):
        raise ValueError('actual Wasm kernel bytes differ')
    additions = ({} if schema_progress else {'bundle_catalog_kernel_runtime.py':
                 (HERE/'bundle_catalog/kernel_runtime.py').read_bytes()})
    changes = {}
    translation = None
    if app_backend:
        additions['app_schema_provider.py'] = (source/'app_provider.py').read_bytes()
        additions['app_schema_model.py'] = (source/'app_schema_model.py').read_bytes()
        name = 'tobkiri_protocol/validation.py'
        original = files[name]
        import_anchor = b'    from jsonschema import Draft202012Validator, FormatChecker, RefResolver\n'
        replacement = b'    from app_schema_provider import AppSchemaProvider as Draft202012Validator\n    from jsonschema import FormatChecker, RefResolver\n'
        if original.count(import_anchor) != 1:
            raise ValueError('exact original App default selection anchor required')
        derived = original.replace(import_anchor, replacement)
        files[name] = derived
        changes[name] = {'original_sha256': hashlib.sha256(original).hexdigest(),
                         'derived_sha256': hashlib.sha256(derived).hexdigest()}
    else:
        additions['schema_adapter.py'] = (source/'adapter.py').read_bytes()
        raw = files['jsonschema/_keywords.py']
        translated, translation = translate(raw)
        files['jsonschema/_keywords.py'] = translated
        additions['jsonschema/_tobkiri_original_keywords.py'] = raw
    if any(name in files for name in additions):
        raise ValueError('derived source collision')
    files.update(additions)
    anchor = b'_schema = importlib.import_module("jsonschema")\n'
    setup = b'''if any(name == "jsonschema" or name.startswith("jsonschema.")
       or name == "_tobkiri_schema_kernel" for name in sys.modules):
    raise RuntimeError("Rust connection must precede first library import")
_kernel = importlib.import_module("_tobkiri_schema_kernel")
if any(name == "jsonschema" or name.startswith("jsonschema.") for name in sys.modules):
    raise RuntimeError("kernel imported the library before source connection")
from schema_adapter import install_standard_classes
install_standard_classes()
if _kernel.keyword_entry_count() != 0:
    raise RuntimeError("schema validation ran during preinit")
'''
    if app_backend:
        setup = b'''if any(name == "jsonschema" or name.startswith("jsonschema.")
       or name == "_tobkiri_schema_kernel" for name in sys.modules):
    raise RuntimeError("App backend must precede first library import")
_kernel = importlib.import_module("_tobkiri_schema_kernel")
if any(name == "jsonschema" or name.startswith("jsonschema.") for name in sys.modules):
    raise RuntimeError("kernel imported library before explicit App selection")
if _kernel.app_schema_entry_count() != 0 or _kernel.app_schema_frame_count() != 0:
    raise RuntimeError("App validation ran during preinit")
'''
    call = b'return read_catalog(embedded_owner())'
    replacement = b'''from bundle_catalog_kernel_runtime import read_catalog_observed
                return read_catalog_observed()'''
    if app_backend:
        replacement = replacement.replace(b'read_catalog_observed()', b'read_catalog_observed(app_backend=True)')
    if entry.count(anchor) != 1 or export.count(call) != 1:
        raise ValueError('exact original entry/export derivation anchors required')
    derived_entry = entry.replace(anchor, setup+anchor)
    derived_export = export.replace(call, replacement)
    if schema_progress:
        derived_export = (HERE/'bundle_catalog/schema_progress.py').read_bytes()
    def identity(data: bytes) -> dict:
        return {'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()}
    return derived_entry, derived_export, artifact.read_bytes(), {
        'format': 'tobkiri.bundle-catalog-kernel-derivation/1',
        'extension_build': fingerprint(report_path, 4*1024*1024),
        'extension': report['artifact'], 'kernel_source': report['source_files'],
        'keyword_translation': translation, 'app_schema_backend': app_backend,
        'schema_progress_diagnostic': schema_progress,
        'app_source_changes': changes, 'features': report['features'],
        'entry': {'original': identity(entry), 'derived': identity(derived_entry)},
        'export': {'original': identity(export), 'derived': identity(derived_export)},
        'additions': {name: identity(raw) for name, raw in additions.items()},
        'validation_results_prepared': False, 'production_enabled': False,
    }
