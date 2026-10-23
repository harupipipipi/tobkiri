"""Run the recorded custom-producer steps on current, verified build outputs.

This script runs only inside the controller's isolated CI namespace. It does
not identify the custom producer as an official componentize-py wheel.
"""
from importlib.metadata import version
import json
from pathlib import Path
import subprocess
import sys

sys.path.insert(0, '/validation-deps')
sys.path.insert(0, '/sdk-python')
import build_component as sdk
from composition import compose

LIMIT = 32 * 1024 * 1024


def capture(path: Path) -> bytes:
    """Capture a generated artifact within the unchanged native Pack limit."""
    if path.is_symlink() or not path.is_file():
        raise ValueError('regular component artifact required')
    with path.open('rb') as stream:
        data = stream.read(LIMIT + 1)
    if not data or len(data) > LIMIT:
        raise ValueError('component artifact exceeds original Pack file bound')
    return data


def run() -> None:
    """Generate bindings, preinitialize Python and check exact typed boundaries."""
    control = json.loads(Path('/control.json').read_bytes())
    compiler = Path('/producer')
    sdk._assert_compiler_unchanged(compiler, control['producer_sha256'])
    if version('wasmtime') != '48.0.0':
        raise ValueError('pinned validation runtime required')
    plan, _, plan_hash, captures = sdk._verify_plan(Path('/inputs/plan.json'))
    if plan_hash != control['plan_sha256']:
        raise ValueError('recorded component plan changed')
    stage = Path('/work/stage')
    stage.mkdir()
    for name in ('home', 'tmp'):
        (Path('/work') / name).mkdir()
    for name, data in captures:
        path = stage.joinpath(*name.parts)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    manifest = [{'source': item['source'], 'destination': str(name),
                 'sha256': sdk.sha256(data), 'bytes': len(data)}
                for item, (name, data) in zip(plan['sources'], captures)]
    env = {'PATH': '/usr/bin:/bin', 'HOME': '/work/home', 'TMPDIR': '/work/tmp',
           'VIRTUAL_ENV': '/empty-venv', 'PYTHONDONTWRITEBYTECODE': '1',
           'PYTHONHASHSEED': '0', 'RAYON_NUM_THREADS': '1', 'TOKIO_WORKER_THREADS': '1'}
    bindings_cmd, component_cmd = sdk._componentize_commands(compiler, plan)
    subprocess.run(bindings_cmd, cwd=stage, env=env, check=True, timeout=180)
    sdk._assert_captured_stage(stage, captures)
    bindings = sdk._tree_manifest(stage, exclude={str(name) for name, _ in captures})
    if not bindings:
        raise ValueError('producer generated no bindings')
    subprocess.run(component_cmd, cwd=stage, env=env, check=True, timeout=600)
    sdk._assert_captured_stage(stage, captures)
    sdk._assert_compiler_unchanged(compiler, control['producer_sha256'])
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
    if (set(inner_imports) != set(plan['allowed_imports'])
            or set(inner_exports) != set(plan['outer_exports']) | {'exports'}):
        raise ValueError('inner component differs from original typed boundary')
    composition = compose(Path('/wac'), inner_path, stage,
                          stage / sdk.destination(plan['wit']).parent,
                          plan['world'], plan['outer_exports'], env)
    sdk._assert_captured_stage(stage, captures)
    if capture(inner_path) != inner:
        raise ValueError('inner component changed during composition')
    outer = capture(stage / 'outer.wasm')
    outer_component = Component(engine, outer)
    imports = sorted(outer_component.type.imports(engine))
    exports = sorted(outer_component.type.exports(engine))
    if set(imports) != set(plan['allowed_imports']) or set(exports) != set(plan['outer_exports']):
        raise ValueError('outer component differs from original typed boundary')
    evidence = {
        'format': 'tobkiri.component-build/2', 'language': 'python',
        'build_mode': 'hash-bound-custom-python313-producer-ci',
        'production_enabled': False, 'guest_runtime_identity_verified': False,
        'compiler_executable_sha256': control['producer_sha256'],
        'producer_ci_receipt_sha256': control['producer_receipt_sha256'],
        'producer_sources': {p.name: sdk.sha256(p.read_bytes()) for p in (
            Path('/driver.py'), Path('/sdk-python/build_component.py'),
            Path('/sdk-python/composition.py'))},
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
        'inner': {'artifact_sha256': sdk.sha256(inner), 'artifact_bytes': len(inner),
                  'imports': inner_imports, 'exports': inner_exports},
        'world': plan['world'], 'imports': imports, 'exports': exports,
        'artifact_sha256': sdk.sha256(outer), 'artifact_bytes': len(outer),
    }
    Path('/work/component.wasm').write_bytes(outer)
    Path('/work/component.build.json').write_text(json.dumps(evidence, indent=2) + '\n')
    print(json.dumps({'artifact_sha256': sdk.sha256(outer), 'artifact_bytes': len(outer),
                      'guest_runtime_identity_verified': False}))


if __name__ == '__main__':
    run()
