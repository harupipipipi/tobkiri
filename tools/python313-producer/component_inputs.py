"""Capture the unchanged historical plans and current original Pack sources."""
import hashlib
import importlib
import json
from pathlib import Path, PurePosixPath
import sys

from build_native import HERE, fingerprint
from fetch_sdk import inventory

PLANS = {
    'usage-cost': ('local.ai-usage-cost-python',
                   'ec95b29b55324381aadcd5906832072b5f1da112ad604db6b39da6b6064cbf63'),
    'stream-normalizer': ('local.ai-stream-normalizer-python',
                          '99d9c6b864bb5ca66f3ac93b2f32408c2d8f1c9946fe19c1b7098001053813a6'),
    'finalizer': ('local.ai-gateway-finalizer-python',
                  '4dc41979980fa297a73983c2947defda66cf32674434b9b3e452ca8f797992d5'),
}
THREAD_CAPABILITIES_WORLD = 'thread-capabilities'
THREAD_CAPABILITIES_EXPORTS = [
    'tobkiri-ui:thread-capabilities/probe@1.0.0',
]
THREAD_CAPABILITIES_IMPORTS = [
    'tobkiri-native:thread-capabilities/bindings@1.0.0',
]
THREAD_CAPABILITIES_SOURCES = {
    'python/app.py': 'tools/python313-producer/harness_thread_capabilities/app.py',
    'python/logic.py': 'tools/python313-producer/harness_thread_capabilities/logic.py',
    'wit/probe.wit': 'tools/python313-producer/harness_thread_capabilities/probe.wit',
    'wit/deps/native-bindings/bindings.wit': (
        'tools/python313-producer/harness_thread_capabilities/'
        'deps/native-bindings/bindings.wit'
    ),
}
THREAD_CAPABILITIES_SHA256 = {
    'python/app.py': 'd26ab3e2562c718193ba0e5c7cdd5d4900029f601ff036624675cb64b2a50050',
    'python/logic.py': '31be4b54eb998763ad79d8f72013942330626823ff3e46b0bde55951aa4118c7',
    'wit/probe.wit': 'a6325c634c50cc656c3af8679840cb316140137a50b960dff20ba0ef0cb2bc34',
    'wit/deps/native-bindings/bindings.wit': (
        '31dceabd9c113f7a0bef41c6f764c35693225b235afc9b2f201c3e351c2458f2'
    ),
}
THREAD_CAPABILITIES_MAX_FILE = 8 * 1024 * 1024
THREAD_CAPABILITIES_MAX_TOTAL = 32 * 1024 * 1024
EXTRA_PLANS = {
    'bridge-v2': ('local.ai-tool-bridge-ordered-python',
                  '4028c00dd67e63c49137440f338fe1995247f9fdbb267a8d0dab7d5cb47b347e'),
    'caller': ('local.ai-gateway-tool-caller-python',
               'bc03deb9d3b214f083258785f2cac52a399aaf3381b1913dea221a21fd4e69ec'),
}
REFERENCES = {
    'python/ordered_json.py': 'packs/local.ai-tool-bridge-ordered-python/ordered_json.py',
    'python/ordered_json_v2.py': 'packs/local.ai-tool-bridge-ordered-python/ordered_json_v2.py',
    'wit/deps/tobkiri-core/core.wit': 'crates/tobkiri-wit/wit/core/core.wit',
    **{f'wit/deps/{name}/pack.wit': f'packs/local.{name}/wit/pack.wit' for name in (
        'ai-gateway-tool-caller-python', 'ai-stream-normalizer-python', 'ai-usage-cost-python')},
    'wit/deps/ai-tool-bridge-ordered-python/pack.wit':
        'packs/local.ai-tool-bridge-ordered-python/wit/pack-v2.wit',
}


def plans(all_five: bool = False) -> dict:
    """Keep the original three-input checkpoint and explicit complete graph."""
    return {**PLANS, **(EXTRA_PLANS if all_five else {})}


def reviewed_stage(repo: Path, name: str) -> dict:
    """Reuse the original source-only stagers, including exact hash-only records."""
    scripts = repo / 'scripts/wasm'
    previous_path = sys.path[:]
    sys.path.insert(0, str(scripts))
    try:
        modules = {'bridge-v2': 'stage_ordered_tool_bridge_v2',
                   'caller': 'stage_gateway_tool_caller'}
        if name not in modules:
            raise ValueError('original reviewed stager required')
        module = importlib.import_module(modules[name])
        if (Path(module.__file__).resolve() != scripts / (modules[name] + '.py')
                or module.ROOT != repo):
            raise ValueError('reviewed stager belongs to a different repository')
        result = module._review()
        files = result[1] if name == 'bridge-v2' else result[0]
    finally:
        sys.path[:] = previous_path
    return files


def canonical(name: str) -> None:
    """Require exact ordinary relative paths, never namespace aliases."""
    if (not name or '\\' in name or ':' in name or '\0' in name
            or PurePosixPath(name).is_absolute()
            or any(part in ('', '.', '..') for part in name.split('/'))):
        raise ValueError('unsafe original component source path')


def capture(repo: Path, name: str) -> tuple:
    """Read only hash-bound plan members, including the exact v2 dependency WIT."""
    if repo.resolve() != repo or name not in plans(True):
        raise ValueError('physical repository and recorded component required')
    pack, plan_sha = plans(True)[name]
    plan_path = HERE / f'trials/2026-10-20-finalization/builds/{name}/plan.json'
    if fingerprint(plan_path, 1024 * 1024)['sha256'] != plan_sha:
        raise ValueError('original component plan changed')
    raw = plan_path.read_bytes()
    plan = json.loads(raw)
    captures, records = {}, []
    reviewed = reviewed_stage(repo, name) if name in EXTRA_PLANS else None
    total = 0
    for item in plan['sources']:
        relative = item['source']
        canonical(relative)
        if relative in captures or relative == 'plan.json':
            raise ValueError('duplicate original component input')
        if reviewed is not None:
            data = reviewed[relative]
            mapped = f'reviewed-source-stager:{name}/{relative}'
            identity = {'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()}
        else:
            mapped = REFERENCES.get(relative, f'packs/{pack}/{relative}')
            path = repo / mapped
            if path.resolve() != path:
                raise ValueError('linked original component input')
            identity = fingerprint(path, 8 * 1024 * 1024)
            with path.open('rb') as stream:
                data = stream.read(8 * 1024 * 1024 + 1)
        if identity['sha256'] != item['sha256']:
            raise ValueError(f'original component input changed: {mapped}')
        total += len(data)
        if (len(data) != identity['bytes'] or hashlib.sha256(data).hexdigest() != identity['sha256']
                or len(data) > 8 * 1024 * 1024
                or total > 32 * 1024 * 1024 or len(captures) >= 256):
            raise ValueError('component input changed or aggregate bound exceeded')
        captures[relative] = data
        records.append({'source': mapped, 'staged': relative, **identity})
    return raw, captures, {'plan_sha256': plan_sha, 'source_files': records}


def stage(repo: Path, name: str, destination: Path) -> dict:
    """Publish a fresh input directory only from already captured original bytes."""
    if not destination.is_absolute() or destination.parent.resolve() != destination.parent:
        raise ValueError('physical new staging destination required')
    raw, captures, receipt = capture(repo, name)
    destination.mkdir(mode=0o700)
    (destination / 'plan.json').write_bytes(raw)
    for relative, data in captures.items():
        path = destination / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open('xb') as output:
            output.write(data)
    files = inventory(destination)
    expected = {'plan.json': raw, **captures}
    if set(files) != set(expected) or any(
            files[path].get('sha256') != hashlib.sha256(data).hexdigest()
            for path, data in expected.items()):
        raise ValueError('staged component source changed')
    return {**receipt, 'files': files}


def stage_harness_setup(repo: Path, destination: Path) -> dict:
    """Stage the current inactive Setup candidate without altering frozen plans.

    The original three/five component inventory remains immutable. This explicit
    candidate uses the same SDK source-plan and isolated producer boundaries.
    """
    if repo != HERE.parents[1] or repo.resolve() != repo:
        raise ValueError('physical current repository required')
    previous_path = sys.path[:]
    sys.path.insert(0, str(repo / 'scripts/wasm'))
    try:
        module = importlib.import_module('prepare_harness_setup')
        if (Path(module.__file__).resolve() != repo / 'scripts/wasm/prepare_harness_setup.py'
                or module.ROOT != repo):
            raise ValueError('Setup stager belongs to another repository')
        plan = module.stage(destination)
    finally:
        sys.path[:] = previous_path
    files = inventory(destination)
    raw = (destination / 'plan.json').read_bytes()
    if json.loads(raw) != plan:
        raise ValueError('Setup source plan changed after staging')
    for item in plan['sources']:
        if files[item['source']]['sha256'] != item['sha256']:
            raise ValueError('Setup source changed after staging')
    return {'plan_sha256': hashlib.sha256(raw).hexdigest(),
            'source_files': plan['sources'], 'files': files}


def stage_harness_native(repo: Path, destination: Path) -> dict:
    """Stage the actual native Harness, separate from every frozen Pack plan."""
    if repo != HERE.parents[1] or repo.resolve() != repo:
        raise ValueError('physical current repository required')
    previous_path = sys.path[:]
    sys.path.insert(0, str(repo / 'scripts/wasm'))
    try:
        module = importlib.import_module('stage_harness_native')
        if (Path(module.__file__).resolve() != repo / 'scripts/wasm/stage_harness_native.py'
                or module.ROOT != repo):
            raise ValueError('native Harness stager belongs to another repository')
        plan = module.stage(destination)
    finally:
        sys.path[:] = previous_path
    files = inventory(destination)
    raw = (destination / 'plan.json').read_bytes()
    if (json.loads(raw) != plan or plan['world'] != 'harness-native'
            or plan['outer_exports'] != [
                'tobkiri-ui:harness-native/runtime@1.0.0',
                'tobkiri-ui:harness-native/transport@1.0.0',
            ] or plan['allowed_imports'] != [
                'tobkiri-native:harness/bindings@1.0.0',
            ] or set(files) != {item['source'] for item in plan['sources']} | {
                'plan.json', 'provenance.json',
            }):
        raise ValueError('native Harness source or boundary changed after staging')
    for item in plan['sources']:
        if files[item['source']]['sha256'] != item['sha256']:
            raise ValueError('native Harness source changed after staging')
    return {'plan_sha256': hashlib.sha256(raw).hexdigest(),
            'source_files': plan['sources'], 'files': files}


def stage_native_input_stack(repo: Path, destination: Path) -> dict:
    """Stage the separate synchronous-stack prerequisite, never an ordinary Pack."""
    if repo != HERE.parents[1] or repo.resolve() != repo:
        raise ValueError('physical current repository required')
    previous_path = sys.path[:]
    sys.path.insert(0, str(repo / 'scripts/wasm'))
    try:
        module = importlib.import_module('prepare_harness_input_stack')
        if (Path(module.__file__).resolve() != repo / 'scripts/wasm/prepare_harness_input_stack.py'
                or module.ROOT != repo):
            raise ValueError('input stack stager belongs to another repository')
        plan = module.stage(destination)
    finally:
        sys.path[:] = previous_path
    files = inventory(destination)
    raw = (destination / 'plan.json').read_bytes()
    if (json.loads(raw) != plan or plan['outer_exports'] != [
            'tobkiri-ui:native-input-stack/probe@1.0.0'] or plan['allowed_imports'] != [
                'tobkiri-ui:inputs/captured@1.0.0']):
        raise ValueError('input stack boundary differs from its independent world')
    for item in plan['sources']:
        if files[item['source']]['sha256'] != item['sha256']:
            raise ValueError('input stack source changed after staging')
    return {'plan_sha256': hashlib.sha256(raw).hexdigest(),
            'source_files': plan['sources'], 'files': files}


def stage_native_input_stack_import_diagnostics(repo: Path, destination: Path) -> dict:
    """Stage only the separate private import-audit diagnostic world."""
    if repo != HERE.parents[1] or repo.resolve() != repo:
        raise ValueError('physical current repository required')
    previous_path = sys.path[:]
    sys.path.insert(0, str(repo / 'scripts/wasm'))
    try:
        module = importlib.import_module(
            'prepare_harness_input_stack_import_diagnostics'
        )
        expected_module = (
            repo / 'scripts/wasm/prepare_harness_input_stack_import_diagnostics.py'
        )
        if Path(module.__file__).resolve() != expected_module or module.ROOT != repo:
            raise ValueError('import diagnostics stager belongs to another repository')
        plan = module.stage(destination)
    finally:
        sys.path[:] = previous_path
    files = inventory(destination)
    raw = (destination / 'plan.json').read_bytes()
    expected_imports = [
        'tobkiri-ui:inputs/captured@1.0.0',
        'tobkiri-ui:diagnostics/import-trace@1.0.0',
    ]
    if (
        json.loads(raw) != plan
        or plan['world'] != 'native-input-stack-import-diagnostics'
        or plan['outer_exports'] != [
            'tobkiri-ui:native-input-stack/probe@1.0.0'
        ]
        or plan['allowed_imports'] != expected_imports
    ):
        raise ValueError('input-stack import diagnostic boundary changed')
    expected_files = {item['source'] for item in plan['sources']} | {
        'plan.json',
        'provenance.json',
    }
    if set(files) != expected_files:
        raise ValueError('input-stack diagnostic staged inventory changed')
    for item in plan['sources']:
        if files[item['source']]['sha256'] != item['sha256']:
            raise ValueError('input-stack diagnostic source changed after staging')
    return {
        'plan_sha256': hashlib.sha256(raw).hexdigest(),
        'source_files': plan['sources'],
        'files': files,
    }


def _capture_thread_capabilities_sources(repo: Path) -> dict[str, bytes]:
    """Capture only the four declared thread-capability diagnostic inputs."""
    captures = {}
    total = 0
    for staged, source in sorted(THREAD_CAPABILITIES_SOURCES.items()):
        path = repo / source
        if path.is_symlink() or not path.is_file() or path.resolve() != path:
            raise ValueError('regular physical thread-capability source required')
        with path.open('rb') as stream:
            data = stream.read(THREAD_CAPABILITIES_MAX_FILE + 1)
        if len(data) > THREAD_CAPABILITIES_MAX_FILE:
            raise ValueError('thread-capability source exceeds its byte bound')
        if hashlib.sha256(data).hexdigest() != THREAD_CAPABILITIES_SHA256[staged]:
            raise ValueError('thread-capability source differs from its reviewed pin')
        total += len(data)
        if total > THREAD_CAPABILITIES_MAX_TOTAL:
            raise ValueError('thread-capability source total exceeds its byte bound')
        captures[staged] = data
    return captures


def _thread_capabilities_plan(files: dict[str, bytes]) -> dict:
    """Build the fixed isolated WIT plan from captured source bytes."""
    if set(files) != set(THREAD_CAPABILITIES_SOURCES):
        raise ValueError('thread-capability source inventory changed')
    sources = [
        {
            'source': name,
            'destination': name.removeprefix('python/'),
            'sha256': hashlib.sha256(content).hexdigest(),
        }
        for name, content in sorted(files.items())
    ]
    return {
        'format': 'tobkiri.python-build/3',
        'stub_wasi': True,
        'wit': 'wit/probe.wit',
        'world': THREAD_CAPABILITIES_WORLD,
        'entrypoint': 'app',
        'sources': sources,
        'outer_exports': THREAD_CAPABILITIES_EXPORTS.copy(),
        'allowed_imports': THREAD_CAPABILITIES_IMPORTS.copy(),
    }


def stage_native_thread_capabilities(repo: Path, destination: Path) -> dict:
    """Stage a source-only native thread/context/termination diagnostic."""
    if repo != HERE.parents[1] or repo.resolve() != repo:
        raise ValueError('physical current repository required')
    if not destination.is_absolute() or destination.parent.resolve() != destination.parent:
        raise ValueError('physical new staging destination required')

    captures = _capture_thread_capabilities_sources(repo)
    plan = _thread_capabilities_plan(captures)
    if _capture_thread_capabilities_sources(repo) != captures:
        raise ValueError('thread-capability source changed during staging')

    raw_plan = (json.dumps(plan, indent=2, sort_keys=True) + '\n').encode('utf-8')
    destination.mkdir(mode=0o700)
    staged_files = {**captures, 'plan.json': raw_plan}
    for relative, data in staged_files.items():
        path = destination / relative
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with path.open('xb') as output:
            output.write(data)

    files = inventory(destination)
    expected = set(staged_files)
    if set(files) != expected or any(
        files[name].get('sha256') != hashlib.sha256(data).hexdigest()
        for name, data in staged_files.items()
    ):
        raise ValueError('staged thread-capability source changed')
    if _capture_thread_capabilities_sources(repo) != captures:
        raise ValueError('thread-capability source changed during staging')
    if (plan['world'] != THREAD_CAPABILITIES_WORLD
            or plan['outer_exports'] != THREAD_CAPABILITIES_EXPORTS
            or plan['allowed_imports'] != THREAD_CAPABILITIES_IMPORTS):
        raise ValueError('thread-capability WIT boundary changed')
    return {
        'plan_sha256': hashlib.sha256(raw_plan).hexdigest(),
        'source_files': plan['sources'],
        'files': files,
    }
