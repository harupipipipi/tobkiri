"""Verify remote original-component artifacts before a separate sharing check."""
import argparse
import hashlib
import json
from pathlib import Path
import stat

from build_native import HERE, STAGED_SHA256, fingerprint
import component_inputs


def document(path: Path) -> dict:
    """Read bounded physical JSON without trusting artifact-relative links."""
    if path.resolve() != path:
        raise ValueError('linked component receipt')
    fingerprint(path, 4 * 1024 * 1024)
    return json.loads(path.read_bytes())


def verify(root: Path, *, all_five: bool = False) -> dict:
    """Bind all three artifacts to original sources and their actual producer."""
    if not root.is_absolute() or root.resolve() != root:
        raise ValueError('physical artifact root required')
    receipt = document(root / 'components-build.json')
    if (receipt.get('format') != 'tobkiri.python313-original-components/1'
            or receipt.get('outcome') != 'success' or not receipt.get('inputs_unchanged')
            or receipt.get('source', {}).get('sha256') != STAGED_SHA256
            or set(receipt.get('components', {})) != set(component_inputs.plans(all_five))
            or receipt.get('native_core_replay') is not False
            or receipt.get('production_enabled') is not False):
        raise ValueError('successful original-component build receipt required')
    expected_files = {'components-build.json'}
    result = {}
    for name, recorded in receipt['components'].items():
        prefix = name + '/work/'
        expected_files.update((prefix + 'component.wasm', prefix + 'component.build.json'))
        proof_path = root / prefix / 'component.build.json'
        artifact = root / prefix / 'component.wasm'
        proof = document(proof_path)
        if (fingerprint(proof_path) != recorded['build_receipt']
                or artifact.resolve() != artifact
                or fingerprint(artifact, 32 * 1024 * 1024) != recorded['artifact']):
            raise ValueError('component artifact or receipt identity changed')
        with artifact.open('rb') as stream:
            if stream.read(8) != b'\0asm\x0d\0\x01\0':
                raise ValueError('actual component encoding required')
        raw, captures, source = component_inputs.capture(HERE.parents[1], name)
        plan = json.loads(raw)
        manifest = [{'source': item['source'], 'destination': item['destination'],
                     'sha256': hashlib.sha256(captures[item['source']]).hexdigest(),
                     'bytes': len(captures[item['source']])} for item in plan['sources']]
        manifest_hash = hashlib.sha256(json.dumps(manifest, sort_keys=True,
            separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()
        if (proof.get('format') != 'tobkiri.component-build/2'
                or proof.get('build_mode') != 'hash-bound-custom-python313-producer-ci'
                or proof.get('compiler_executable_sha256') != receipt['producer']['sha256']
                or proof.get('producer_ci_receipt_sha256') != receipt['producer_receipt']['sha256']
                or proof.get('build_plan_sha256') != source['plan_sha256']
                or proof.get('source_manifest') != manifest
                or proof.get('source_manifest_sha256') != manifest_hash
                or proof.get('artifact_sha256') != recorded['artifact']['sha256']
                or proof.get('artifact_bytes') != recorded['artifact']['bytes']
                or set(proof.get('imports', [])) != set(plan['allowed_imports'])
                or set(proof.get('exports', [])) != set(plan['outer_exports'])
                or proof.get('production_enabled') is not False
                or proof.get('guest_runtime_identity_verified') is not False):
            raise ValueError('component source or producer provenance changed')
        result[name] = {'artifact': recorded['artifact'], 'source': source,
                        'build_receipt': recorded['build_receipt']}
    actual = set()
    for path in root.rglob('*'):
        mode = path.lstat().st_mode
        if stat.S_ISDIR(mode):
            continue
        if not stat.S_ISREG(mode):
            raise ValueError('nonregular component artifact entry')
        actual.add(path.relative_to(root).as_posix())
    if actual != expected_files:
        raise ValueError('remote component artifact inventory changed')
    return {'components': result, 'producer': receipt['producer'],
            'receipt': fingerprint(root / 'components-build.json'),
            'native_core_acceptance': False, 'production_enabled': False}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--all-five', action='store_true')
    args = parser.parse_args()
    print(json.dumps(verify(args.root, all_five=args.all_five), indent=2))
