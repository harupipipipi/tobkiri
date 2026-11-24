"""Bind the complete original graph and export inputs for the native Core test."""
import argparse
import json
from pathlib import Path
import stat

from build_native import HERE, fingerprint
from verify_original_components import document, verify

ROLES = (('USAGE', 'usage-cost'), ('STREAM', 'stream-normalizer'),
         ('BRIDGE', 'bridge-v2'), ('CALLER', 'caller'), ('FINALIZER', 'finalizer'))
PREFIX = 'TOBKIRI_TEST_RUST_PROVIDERS_'


def check(path: Path, entry: dict) -> dict:
    """Require exact physical bounded bytes before passing a fixture path."""
    if path.resolve() != path or not 8 <= entry['bytes'] <= 32 * 1024 * 1024:
        raise ValueError('linked native fixture input')
    identity = fingerprint(path, 32 * 1024 * 1024)
    if identity != {key: entry[key] for key in ('bytes', 'sha256')}:
        raise ValueError('native fixture identity changed')
    return identity


def values(root: Path, provider: Path, bridge: Path, evidence: Path) -> dict:
    """Verify all code and current source; this never executes or approves it."""
    artifacts = root / 'original-five-artifacts'
    originals = verify(artifacts, all_five=True)
    shared = root / 'original-five-shared'
    receipt_path = shared / 'extraction.json'
    receipt = document(receipt_path)
    if (receipt.get('format') != 'tobkiri.shared-core-module-materialization/1'
            or receipt.get('guest_execution_performed') is not False
            or receipt.get('native_core_acceptance') is not False
            or receipt.get('production_enabled') is not False
            or len(receipt.get('components', [])) != len(ROLES)):
        raise ValueError('complete inactive shared-code receipt required')
    expected_files = {'extraction.json'}
    unique, roots = {}, 0
    output = {}
    for index, (role, name) in enumerate(ROLES):
        record = receipt['components'][index]
        identity = originals['components'][name]['artifact']
        if record['source_sha256'] != identity['sha256'] or record['source_bytes'] != identity['bytes']:
            raise ValueError('shared code belongs to a different original or role order')
        relative = f'{index}/component.wasm'
        transformed = record['component']
        check(shared / relative, transformed)
        expected_files.add(relative)
        roots += transformed['bytes']
        modules = transformed['modules']
        if not 1 <= len(modules) <= 256:
            raise ValueError('shared module count exceeded')
        own = {}
        for module_index, module in enumerate(modules):
            if module['import'] != f'unbundled-module{module_index}':
                raise ValueError('shared module import order changed')
            relative = f'{index}/module-{module_index}.wasm'
            check(shared / relative, module)
            expected_files.add(relative)
            if unique.setdefault(module['sha256'], module['bytes']) != module['bytes']:
                raise ValueError('inconsistent shared code identity')
            own[module['sha256']] = module['bytes']
        if transformed['bytes'] + sum(own.values()) > 32 * 1024 * 1024:
            raise ValueError('original Pack source limit exceeded')
        output[PREFIX + role + '_COMPONENT'] = str(artifacts / name / 'work/component.wasm')
        output[PREFIX + role + '_SHA256'] = identity['sha256']
        output[PREFIX + role + '_IMPLEMENTATION'] = 'python'
    total = roots + sum(unique.values())
    if (total != receipt['catalog_source_bytes'] or total > 64 * 1024 * 1024
            or receipt['unique_modules'] != len(unique)):
        raise ValueError('unchanged original catalog bound or source accounting differs')
    actual = set()
    for path in shared.rglob('*'):
        mode = path.lstat().st_mode
        if stat.S_ISDIR(mode):
            continue
        if not stat.S_ISREG(mode):
            raise ValueError('nonregular shared-code input')
        actual.add(path.relative_to(shared).as_posix())
    if actual != expected_files:
        raise ValueError('unexpected shared-code inventory')
    repo = HERE.parents[1]
    paths = {'ORACLE': provider, 'BRIDGE_ORACLE': bridge,
             'FINALIZER_ORACLE': repo / 'tools/python313-producer/trials/2026-10-20-finalization/source/finalizer.oracle.json',
             'CALLER_ORACLE': repo / 'tools/python313-producer/trials/2026-10-20-current-caller/source-observations.json',
             'SHARED_MODULES': shared}
    for name, path in paths.items():
        if name == 'SHARED_MODULES':
            output[PREFIX + name + '_DIR'] = str(path)
            output[PREFIX + name + '_SHA256'] = fingerprint(receipt_path)['sha256']
        else:
            if path.resolve() != path:
                raise ValueError('linked source oracle')
            output[PREFIX + name] = str(path)
            output[PREFIX + name + '_SHA256'] = fingerprint(path, 4 * 1024 * 1024)['sha256']
    if (not evidence.is_absolute() or evidence.exists() or evidence.is_symlink()
            or not evidence.parent.is_dir() or evidence.parent.resolve() != evidence.parent):
        raise ValueError('fresh physical native evidence destination required')
    output[PREFIX + 'COMPOSITION_EVIDENCE_DIR'] = str(evidence)
    if any('\n' in value or '\r' in value for value in output.values()):
        raise ValueError('invalid native environment value')
    return output


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    for name in ('root', 'provider', 'bridge', 'evidence', 'github-env'):
        parser.add_argument('--' + name, type=Path, required=True)
    args = parser.parse_args()
    environment = values(args.root, args.provider, args.bridge, args.evidence)
    with args.github_env.open('a') as target:
        for name, value in environment.items():
            target.write(f'{name}={value}\n')
    print(json.dumps({'original_python_packs': 5, 'native_core_acceptance': False,
                      'inputs_bound': True, 'test': 'all 215 original cases'}))
