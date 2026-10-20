"""Hash-bound development producer trial; never an official-wheel identity."""
from pathlib import Path
import hashlib
from importlib.metadata import version
import json
import os
import subprocess
import sys

sys.path.insert(0, '/validation-deps')
sys.path.insert(0, '/sdk-python')
import build_component as sdk
from composition import compose

EXPECTED_CLI = 'ba5ce27a1109faeaf80d0f1fe3491f1863a3bb67db08c11845151e55cd483987'
EXPECTED_PLAN = '4dc41979980fa297a73983c2947defda66cf32674434b9b3e452ca8f797992d5'
LIMIT = 128 * 1024 * 1024
sha = lambda data: hashlib.sha256(data).hexdigest()

def capture(path):
    if path.is_symlink() or not path.is_file() or path.stat().st_size > LIMIT:
        raise ValueError('invalid component file')
    with path.open('rb') as stream:
        data = stream.read(LIMIT + 1)
    if not data or len(data) > LIMIT:
        raise ValueError('component exceeds native Pack member limit')
    return data

assert version('wasmtime') == '48.0.0'
compiler = Path('/producer')
assert sha(compiler.read_bytes()) == EXPECTED_CLI
plan, plan_bytes, plan_hash, captures = sdk._verify_plan(Path('/inputs/plan.json'))
assert plan_hash == EXPECTED_PLAN
stage = Path('/work/stage')
stage.mkdir()
for name, data in captures:
    path = stage.joinpath(*name.parts)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
manifest = [{'source': item['source'], 'destination': str(name),
             'sha256': sha(data), 'bytes': len(data)}
            for item, (name, data) in zip(plan['sources'], captures)]
env = {'PATH': '/usr/bin:/bin', 'HOME': '/work/home', 'TMPDIR': '/work/tmp',
       'VIRTUAL_ENV': '/empty-venv', 'PYTHONDONTWRITEBYTECODE': '1',
       'PYTHONHASHSEED': '0', 'RAYON_NUM_THREADS': '1', 'TOKIO_WORKER_THREADS': '1'}
bindings_cmd, component_cmd = sdk._componentize_commands(compiler, plan)
subprocess.run(bindings_cmd, cwd=stage, env=env, check=True, timeout=180)
sdk._assert_captured_stage(stage, captures)
bindings = sdk._tree_manifest(stage, exclude={str(name) for name, _ in captures})
assert bindings
subprocess.run(component_cmd, cwd=stage, env=env, check=True, timeout=600)
sdk._assert_captured_stage(stage, captures)
sdk._assert_compiler_unchanged(compiler, EXPECTED_CLI)
from wasmtime import Config, Engine
from wasmtime.component import Component
config = Config()
config.parallel_compilation = False
engine = Engine(config)
inner_path = stage / 'component.wasm'
inner = capture(inner_path)
inner_component = Component(engine, inner)
inner_imports = sorted(inner_component.type.imports(engine))
inner_exports = sorted(inner_component.type.exports(engine))
assert set(inner_imports) == set(plan['allowed_imports'])
assert set(inner_exports) == set(plan['outer_exports']) | {'exports'}
composition = compose(Path('/wac'), inner_path, stage,
                      stage / sdk.destination(plan['wit']).parent,
                      plan['world'], plan['outer_exports'], env)
sdk._assert_captured_stage(stage, captures)
assert capture(inner_path) == inner
outer = capture(stage / 'outer.wasm')
outer_component = Component(engine, outer)
imports = sorted(outer_component.type.imports(engine))
exports = sorted(outer_component.type.exports(engine))
assert set(imports) == set(plan['allowed_imports'])
assert set(exports) == set(plan['outer_exports'])
evidence = {
    'format': 'tobkiri.component-build/2', 'language': 'python',
    'build_mode': 'hash-bound-custom-python313-producer-trial',
    'production_enabled': False, 'guest_runtime_identity_verified': False,
    'compiler_executable_sha256': EXPECTED_CLI,
    'producer_trial_receipt_sha256': sha(Path('/producer-receipt.json').read_bytes()),
    'producer_sources': {p.name: sha(p.read_bytes()) for p in
                         [Path('/driver.py'), Path('/sdk-python/build_component.py'),
                          Path('/sdk-python/composition.py')]},
    'build_plan_sha256': plan_hash, 'source_manifest': manifest,
    'source_manifest_sha256': sdk.canonical_hash(manifest),
    'bindings': {'files': bindings, 'manifest_sha256': sdk.canonical_hash(bindings)},
    'options': {'stub_wasi': True, 'inherited_environment': False,
                'site_packages': 'explicit empty private VIRTUAL_ENV',
                'compiler_process_pythonhashseed': '0',
                'guest_pythonhashseed': 'upstream compiler default uncontrolled'},
    'commands': {'bindings': bindings_cmd, 'componentize': component_cmd},
    'composition': composition,
    'target_validation': {'tool': 'wac-cli', 'world': plan['world'],
                          'exact_exports': True, 'typed_target_check': True},
    'inner': {'artifact_sha256': sha(inner), 'artifact_bytes': len(inner),
              'imports': inner_imports, 'exports': inner_exports},
    'world': plan['world'], 'imports': imports, 'exports': exports,
    'artifact_sha256': sha(outer), 'artifact_bytes': len(outer),
}
Path('/work/component.wasm').write_bytes(outer)
Path('/work/component.build.json').write_text(json.dumps(evidence, indent=2) + '\n')
print(json.dumps({'artifact_sha256': sha(outer), 'artifact_bytes': len(outer),
                  'guest_runtime_identity_verified': False}))
