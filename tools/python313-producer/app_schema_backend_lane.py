"""Authenticate the separately built experimental App backend before first use."""
from __future__ import annotations
import importlib.util
import json
import os
from pathlib import Path
import sys

HERE = Path(__file__).resolve().parent


def install(precheck: Path, packages: list[Path]):
    from app_schema_oracle import physical, require_ci
    from build_native import fingerprint
    from fetch_sdk import inventory
    require_ci()
    physical(precheck)
    path = precheck/'schema-kernel-native-check.json'
    physical(path)
    if path.stat().st_size > 4*1024*1024:
        raise ValueError('bounded App backend receipt required')
    receipt = json.loads(path.read_bytes())
    source = HERE/'schema_kernel'
    if (receipt.get('format') != 'tobkiri.schema-kernel-native-check/1'
            or receipt.get('outcome') != 'success'
            or receipt.get('source_head') != os.environ.get('GITHUB_SHA')
            or receipt.get('source_files') != inventory(source)
            or receipt.get('features') != ['app-schema-backend']
            or receipt.get('app_backend_64_success') is not True
            or receipt.get('production_enabled') is not False):
        raise ValueError('successful actual App backend precheck required')
    extension = precheck/'module/_tobkiri_schema_kernel.abi3.so'
    physical(extension)
    if fingerprint(extension, 32*1024*1024) != receipt['extension']:
        raise ValueError('actual App backend extension differs')
    if any(n == 'jsonschema' or n.startswith('jsonschema.') or n == '_tobkiri_schema_kernel'
           for n in sys.modules):
        raise ValueError('App backend must precede first import')
    sys.path[:0] = [str(extension.parent), *(str(p) for p in packages)]
    import _tobkiri_schema_kernel as kernel
    import jsonschema
    original = [p for p in packages if p.name == 'jsonschema']
    if (len(original) != 1 or Path(jsonschema.__file__).resolve(strict=True)
            != original[0]/'jsonschema/__init__.py'
            or Path(kernel.__file__).resolve(strict=True) != extension):
        raise ValueError('actual original library and compiled backend origins required')
    spec = importlib.util.spec_from_file_location('app_schema_provider', source/'app_provider.py')
    if spec is None or spec.loader is None or spec.name in sys.modules:
        raise ValueError('fresh actual provider source required')
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    if kernel.app_schema_entry_count() != 0 or kernel.app_schema_frame_count() != 0:
        raise ValueError('App workload executed during provider initialization')
    return kernel, packages, {'extension': receipt['extension'],
        'source_files': receipt['source_files'], 'features': receipt['features'],
        'app_backend_64_success': True, 'precheck_receipt': fingerprint(path, 4*1024*1024),
        'document_backend': 'original-preselected-operation'}


def bind(module):
    """Select the App-owned default at the real, still-cold dependency seam."""
    from jsonschema import Draft202012Validator
    from app_schema_provider import AppSchemaProvider
    if (module.Draft202012Validator is not Draft202012Validator
            or module.load_schema.cache_info().currsize != 0
            or module._schema_store.cache_info().currsize != 0):
        raise ValueError('App default was already replaced or used before selection')
    module.Draft202012Validator = AppSchemaProvider
