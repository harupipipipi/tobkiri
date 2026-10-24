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
