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


def derive(files: dict[str, bytes], entry: bytes, export: bytes,
           extension_root: Path) -> tuple[bytes, bytes, bytes, dict]:
    """Add only the actual kernel/adapter and declared pre-import translation."""
    if (os.environ.get('GITHUB_ACTIONS') != 'true'
            or not extension_root.is_absolute()
            or extension_root.resolve(strict=True) != extension_root):
        raise ValueError('physical CI extension build required')
    report_path = extension_root/'extension-build.json'
    report = json.loads(report_path.read_bytes())
    source = HERE/'schema_kernel'
    if (report.get('format') != 'tobkiri.app-schema-kernel-extension/1'
            or report.get('outcome') != 'success'
            or report.get('guest_executed') is not False
            or report.get('production_enabled') is not False
            or report.get('source_head') != os.environ.get('GITHUB_SHA')
            or report.get('source_files') != inventory(source)
            or report.get('artifact_path')
            != 'work/target/wasm32-wasip2/release/_tobkiri_schema_kernel.wasm'):
        raise ValueError('actual source-built kernel derivation required')
    artifact = extension_root/report['artifact_path']
    if (artifact.resolve(strict=True) != artifact
            or fingerprint(artifact, 4*1024*1024) != report['artifact']):
        raise ValueError('actual Wasm kernel bytes differ')
    additions = {'schema_adapter.py': (source/'adapter.py').read_bytes(),
                 'bundle_catalog_kernel_runtime.py':
                 (HERE/'bundle_catalog/kernel_runtime.py').read_bytes()}
    if any(name in files for name in (*additions, 'jsonschema/_tobkiri_original_keywords.py')):
        raise ValueError('derived source collision')
    raw = files['jsonschema/_keywords.py']
    translated, translation = translate(raw)
    files['jsonschema/_keywords.py'] = translated
    files['jsonschema/_tobkiri_original_keywords.py'] = raw
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
    call = b'return read_catalog(embedded_owner())'
    replacement = b'''from bundle_catalog_kernel_runtime import read_catalog_observed
                return read_catalog_observed()'''
    if entry.count(anchor) != 1 or export.count(call) != 1:
        raise ValueError('exact original entry/export derivation anchors required')
    derived_entry = entry.replace(anchor, setup+anchor)
    derived_export = export.replace(call, replacement)
    def identity(data: bytes) -> dict:
        return {'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()}
    return derived_entry, derived_export, artifact.read_bytes(), {
        'format': 'tobkiri.bundle-catalog-kernel-derivation/1',
        'extension_build': fingerprint(report_path, 4*1024*1024),
        'extension': report['artifact'], 'kernel_source': report['source_files'],
        'keyword_translation': translation,
        'entry': {'original': identity(entry), 'derived': identity(derived_entry)},
        'export': {'original': identity(export), 'derived': identity(derived_export)},
        'additions': {name: identity(raw) for name, raw in additions.items()},
        'validation_results_prepared': False, 'production_enabled': False,
    }
