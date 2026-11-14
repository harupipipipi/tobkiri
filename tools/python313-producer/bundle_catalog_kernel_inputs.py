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
PHASE_COMPONENT = 'harness-app-schema-phases-native'
PHASE_IMPORT = 'tobkiri-native:app-schema-phases/bindings@1.0.0'
PHASE_RECEIPT = 'tobkiri.app-schema-phases-native-diagnostic/1'


def component_name(*, app_backend: bool, schema_progress: bool = False,
                   schema_phases: bool = False) -> str:
    """Select a closed, distinct diagnostic artifact without changing defaults."""
    if (type(app_backend) is not bool or type(schema_progress) is not bool
            or type(schema_phases) is not bool
            or (schema_progress and schema_phases)
            or ((schema_progress or schema_phases) and not app_backend)):
        raise ValueError('select at most one diagnostic with the explicit App backend')
    if schema_phases:
        return PHASE_COMPONENT
    return PROGRESS_COMPONENT if schema_progress else APP_COMPONENT if app_backend else COMPONENT


def _replace_one(raw: bytes, anchor: bytes, replacement: bytes) -> bytes:
    if raw.count(anchor) != 1:
        raise ValueError('exact original App schema phase source anchor required')
    return raw.replace(anchor, replacement)


def _observe_provider(provider: bytes) -> bytes:
    if b'import app_schema_phases as _phases\n' in provider:
        raise ValueError('exact original App schema phase provider required')
    provider = _replace_one(provider, b'from app_schema_model import DefinitionModel\n',
                            b'from app_schema_model import DefinitionModel\n'
                            b'import app_schema_phases as _phases\n')
    call = (b'        kernel.app_check_schema(schema, meta, cls._REGISTRY,\n'
            b'                                _REFERENCE_POLICY, cls.TYPE_CHECKER, checker,\n'
            b'                                value_policy=_VALUE_POLICY, type_owner=cls)')
    observed = b'''        if _phases.observing():
            return kernel.app_check_schema_with_phases(
                schema, meta, cls._REGISTRY, _REFERENCE_POLICY, cls.TYPE_CHECKER,
                checker, _phases.mark, _phases.seed(),
                value_policy=_VALUE_POLICY, type_owner=cls)
'''
    return _replace_one(provider, call, observed + call)


def _observe_cold_call(export: bytes) -> bytes:
    if b'import app_schema_phases as _phases\n' in export:
        raise ValueError('exact original App schema phase export required')
    export = _replace_one(export, b'import wit_world.exports\n',
                          b'import wit_world.exports\n'
                          b'import app_schema_phases as _phases\n')
    call = b'                schemas = _schema_store()\n'
    wrapped = b'''                _phases.begin_cold_schema_store()
                try:
                    schemas = _schema_store()
                finally:
                    _phases.end_cold_schema_store()
'''
    return _replace_one(export, call, wrapped)


def _observe_catalog_reads(runtime: bytes) -> bytes:
    runtime = _replace_one(runtime, b'import sys\n',
                           b'import sys\nfrom wit_world.imports.bindings import checkpoint\n')
    schema = b'            schema_reads.append(path.name)\n'
    runtime = _replace_one(runtime, schema, schema +
                           b'            checkpoint(f"catalog:schema:{path.name}")\n')
    runtime = _replace_one(runtime, b'        actual = app.read_catalog(app.embedded_owner())\n',
                           b'''        checkpoint("catalog:phase:owner-start")
        owner = app.embedded_owner()
        checkpoint("catalog:phase:owner-return")
        actual = app.read_catalog(owner)
        checkpoint("catalog:phase:loader-return")
''')
    read = (b'            raise RuntimeError(\'actual entry read observation exceeded its bound\')\n')
    return _replace_one(runtime, read, read + b'''        if reads == 453 or (reads & (reads - 1)) == 0:
            checkpoint(f"catalog:entry:{reads}")
''')


def derive(files: dict[str, bytes], entry: bytes, export: bytes,
           extension_root: Path, *, app_backend: bool = False,
           schema_progress: bool = False,
           schema_phases: bool = False) -> tuple[bytes, bytes, bytes, dict]:
    """Add only the actual kernel/adapter and declared pre-import translation."""
    if (type(app_backend) is not bool
            or os.environ.get('GITHUB_ACTIONS') != 'true'
            or not extension_root.is_absolute()
            or extension_root.resolve(strict=True) != extension_root):
        raise ValueError('physical CI extension build required')
    component_name(app_backend=app_backend, schema_progress=schema_progress,
                   schema_phases=schema_phases)
    features = ['app-schema-backend'] if app_backend else []
    if schema_phases:
        features.append('app-schema-phases')
    report_path = extension_root/'extension-build.json'
    report = json.loads(report_path.read_bytes())
    source = HERE/'schema_kernel'
    if (report.get('format') != 'tobkiri.app-schema-kernel-extension/1'
            or report.get('outcome') != 'success'
            or report.get('guest_executed') is not False
            or report.get('production_enabled') is not False
            or report.get('source_head') != os.environ.get('GITHUB_SHA')
            or report.get('features') != features
            or (schema_phases and set(report.get('compiled_features', ()))
                != {'default', *features})
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
    def identity(data: bytes) -> dict:
        return {'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()}

    translation = None
    if app_backend:
        provider = (source/'app_provider.py').read_bytes()
        additions['app_schema_provider.py'] = provider
        additions['app_schema_model.py'] = (source/'app_schema_model.py').read_bytes()
        name = 'tobkiri_protocol/validation.py'
        original = files[name]
        import_anchor = b'    from jsonschema import Draft202012Validator, FormatChecker, RefResolver\n'
        replacement = b'    from app_schema_provider import AppSchemaProvider as Draft202012Validator\n    from jsonschema import FormatChecker, RefResolver\n'
        if original.count(import_anchor) != 1:
            raise ValueError('exact original App default selection anchor required')
        derived = original.replace(import_anchor, replacement)
        if schema_phases:
            additions['app_schema_phases.py'] = (
                HERE/'bundle_catalog/schema_phases.py').read_bytes()
            runtime_name = 'bundle_catalog_kernel_runtime.py'
            additions[runtime_name] = _observe_catalog_reads(additions[runtime_name])
            additions['app_schema_provider.py'] = _observe_provider(provider)
            changes['app_schema_provider.py'] = {
                'original_sha256': identity(provider)['sha256'],
                'derived_sha256': identity(additions['app_schema_provider.py'])['sha256'],
            }
            derived = _replace_one(
                derived, replacement,
                replacement + b'    from app_schema_phases import bind_schema as _bind_schema\n')
            call = b'    try:\n        Draft202012Validator.check_schema(payload)\n'
            derived = _replace_one(derived, call,
                                   b'    _bind_schema(schema_name)\n' + call)
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
    if schema_phases:
        derived_export = _observe_cold_call(derived_export)
    return derived_entry, derived_export, artifact.read_bytes(), {
        'format': 'tobkiri.bundle-catalog-kernel-derivation/1',
        'extension_build': fingerprint(report_path, 4*1024*1024),
        'extension': report['artifact'], 'kernel_source': report['source_files'],
        'keyword_translation': translation, 'app_schema_backend': app_backend,
        'schema_progress_diagnostic': schema_progress,
        **({'schema_phase_diagnostic': True, 'phase_import': PHASE_IMPORT}
           if schema_phases else {}),
        'app_source_changes': changes, 'features': report['features'],
        'entry': {'original': identity(entry), 'derived': identity(derived_entry)},
        'export': {'original': identity(export), 'derived': identity(derived_export)},
        'additions': {name: identity(raw) for name, raw in additions.items()},
        'validation_results_prepared': False, 'production_enabled': False,
    }
