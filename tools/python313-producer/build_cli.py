"""Rebuild the original producer CLI through its recorded publishing recipe."""
import argparse
import json
import os
from pathlib import Path
import sys

from build_assets import verify_outputs
from build_native import GIB, HERE, fingerprint, sandbox_command
from build_runtimes import CARGO_CONFIG
from build_wasi import run_stage
import cli_source
import ci_link_trace_source
import fetch_sdk
import runtime_inputs
import ui_clock_source
import ui_monotonic_source
import ui_empty_poll_assets
from verify_git_checkout import verify_cache

RECIPE_SHA256 = '330ccaa93ee1feba18b881eaeac70fc77013bddd8ef2128e9c7fb866528d1fa4'
STARTUP_RECIPE = '''set -eu
/usr/bin/timeout --kill-after=5 15 /producer --version
/usr/bin/timeout --kill-after=5 15 /producer --help
'''


def run(source: Path, registry: Path, rust: Path, git: Path, wasi: Path,
        runtimes: Path, assets: Path, destination: Path, *,
        allow_ui_wall_clock: bool = False,
        allow_ui_monotonic_clock: bool = False,
        allow_ci_link_trace: bool = False,
        allow_inactive_ui_libc: bool = False) -> None:
    """Compile only with immutable verified source/assets and offline caches."""
    if sys.platform != 'linux' or os.environ.get('GITHUB_ACTIONS') != 'true':
        raise ValueError('producer CLI rebuilding is restricted to Linux CI')
    if allow_ui_monotonic_clock and not allow_ui_wall_clock:
        raise ValueError('monotonic candidate requires the explicit wall-clock mode')
    if (not destination.is_absolute() or not destination.parent.is_dir()
            or destination.parent.resolve() != destination.parent
            or any(not p.is_absolute() or p.resolve() != p for p in (runtimes, assets))):
        raise ValueError('physical input and new destination paths required')
    destination.mkdir(mode=0o700)
    inputs, snapshots = runtime_inputs.verify(source, registry, rust, git, wasi)
    asset_receipt_path = assets / 'assets-build.json'
    asset_report = runtime_inputs.report(asset_receipt_path)
    inactive_libc_marker = ui_empty_poll_assets.require_cli_mode(asset_report, allow_inactive_ui_libc)
    runtime_receipt_path = runtimes / 'runtimes-build.json'
    runtime_report = runtime_inputs.report(runtime_receipt_path)
    if (asset_report['inputs'] != inputs or not asset_report['inputs_unchanged']
            or runtime_report['inputs'] != inputs or not runtime_report['inputs_unchanged']
            or asset_report['runtime_receipt'] != fingerprint(runtime_receipt_path)):
        raise ValueError('producer predecessor receipts do not match verified inputs')
    packaged = assets / 'work/assets'
    if (fingerprint(packaged / 'package-receipt.json') != asset_report['package_receipt']
            or verify_outputs(packaged, asset_report['manifest']['sha256'],
                              asset_report['worker']['sha256']) != asset_report['outputs']):
        raise ValueError('producer assets differ from successful packaging')
    cache = runtimes / 'work/runtime-cargo-home/git'
    runtime_inputs.recorded_files(cache, runtime_report['git_cache_inventory'])
    checkout = verify_cache(cache.parent, git / 'work')
    snapshots[str(cache)] = runtime_report['git_cache_inventory']
    snapshots[str(packaged)] = fetch_sdk.inventory(packaged)
    stage = destination / 'source'
    source_receipt = cli_source.stage(source / 'componentize-py', packaged,
                                     asset_report['outputs'], stage)
    if allow_ui_wall_clock:
        source_receipt = ui_clock_source.enable(stage, source_receipt)
    if allow_ui_monotonic_clock:
        source_receipt = ui_monotonic_source.enable(stage, source_receipt)
    if allow_ci_link_trace:
        source_receipt = ci_link_trace_source.enable(stage, source_receipt)
    snapshots[str(stage)] = source_receipt['files']
    (destination / 'cli-source.json').write_text(json.dumps(source_receipt, indent=2) + '\n')
    recipe = HERE / 'trials/2026-10-19/producer-cli.sh'
    if fingerprint(recipe)['sha256'] != RECIPE_SHA256:
        raise ValueError('original producer CLI recipe changed')
    candidate_recipe_identity = None
    trace_predecessor_recipe = None
    if allow_ui_wall_clock:
        candidate_recipe = destination / (
            'ui-monotonic-cli.sh' if allow_ui_monotonic_clock else 'ui-clock-cli.sh')
        recipe_bytes = ui_clock_source.build_recipe(recipe.read_bytes())
        if allow_ui_monotonic_clock:
            recipe_bytes = ui_monotonic_source.build_recipe(recipe_bytes)
        candidate_recipe.write_bytes(recipe_bytes)
        candidate_recipe_identity = fingerprint(candidate_recipe)
        recipe = candidate_recipe
    if allow_ci_link_trace:
        trace_predecessor_recipe = fingerprint(recipe)
        candidate_recipe = destination / 'ci-link-trace-cli.sh'
        candidate_recipe.write_bytes(ci_link_trace_source.build_recipe(recipe.read_bytes()))
        candidate_recipe_identity = fingerprint(candidate_recipe)
        recipe = candidate_recipe
    work = destination / 'work'
    work.mkdir(mode=0o700)
    (work / 'cargo-home').mkdir(mode=0o700)
    config = work / 'cargo-home/config.toml'
    config.write_text(CARGO_CONFIG)
    config_identity = fingerprint(config)
    predecessor_identities = {str(p): fingerprint(p) for p in (
        asset_receipt_path, runtime_receipt_path, destination / 'cli-source.json')}
    receipt = {'format': 'tobkiri.python313-cli-rebuild/1', 'outcome': 'pending',
               'inputs': inputs, 'predecessors': predecessor_identities,
               'original_recipe_sha256': RECIPE_SHA256, 'verified_git_checkout': checkout,
               'source_delta': source_receipt['delta'], 'stages': [],
               'guest_execution': False, 'componentization': False,
               'production_enabled': False, 'historical_binary_identity_claimed': False}
    if allow_ui_wall_clock:
        receipt['ui_wall_clock_candidate'] = source_receipt['ui_wall_clock']
        receipt['candidate_recipe'] = candidate_recipe_identity
    if inactive_libc_marker is not None:
        receipt['ui_empty_poll_libc_candidate'] = inactive_libc_marker
    if allow_ui_monotonic_clock:
        receipt['ui_monotonic_clock_candidate'] = source_receipt['ui_monotonic_clock']
    if allow_ci_link_trace:
        receipt['ci_link_trace_candidate'] = source_receipt['ci_link_trace']
        receipt['ci_link_trace_predecessor_recipe'] = trace_predecessor_recipe
        receipt['candidate_recipe'] = candidate_recipe_identity
    receipt_path = destination / 'cli-build.json'
    try:
        mounts = (
            (stage, '/source/componentize-py'), (registry / 'sources', '/registry/sources'),
            (rust / 'toolchain', '/rust'), (config, '/work/cargo-home/config.toml'),
            (cache, '/work/cargo-home/git'),
            # Preserve any Git alternates recorded by the runtime cache primer.
            (cache, '/work/runtime-cargo-home/git'),
            (git / 'work/wasm-tools.git', '/git-input/wasm-tools.git'),
            (git / 'work/testsuite.git', '/git-input/testsuite.git'),
        )
        command = sandbox_command(source, work, recipe, mounts, address_space_bytes=4 * GIB)
        receipt['stages'].append({'name': 'producer-cli',
            'supervision': run_stage('producer-cli', command, work, destination / 'producer-cli')})
        binary = work / 'target/release/componentize-py'
        identity = fingerprint(binary, 128 * 1024 * 1024)
        startup = destination / 'startup.sh'
        startup.write_text(STARTUP_RECIPE)
        command = sandbox_command(source, work, startup, ((binary, '/producer'),),
                                  address_space_bytes=4 * GIB)
        receipt['stages'].append({'name': 'cli-startup', 'recipe': fingerprint(startup),
            'supervision': run_stage('cli-startup', command, work, destination / 'cli-startup')})
        log = destination / 'cli-startup/build.log'
        fingerprint(log, 64 * 1024)
        if not log.read_text().startswith('componentize-py 0.25.0\n'):
            raise ValueError('rebuilt producer reports an unexpected version')
        runtime_inputs.unchanged(source, inputs, snapshots)
        for path, expected in predecessor_identities.items():
            if fingerprint(Path(path)) != expected:
                raise ValueError('producer predecessor identity changed')
        if fingerprint(config) != config_identity or fingerprint(binary) != identity:
            raise ValueError('producer configuration or binary changed during startup')
        if candidate_recipe_identity is not None and fingerprint(recipe) != candidate_recipe_identity:
            raise ValueError('UI clock producer recipe changed')
        receipt.update(outcome='success', binary=identity, inputs_unchanged=True,
                       producer_cli=True, reported_version='componentize-py 0.25.0')
    except BaseException:
        receipt['outcome'] = 'failed'
        raise
    finally:
        receipt_path.write_text(json.dumps(receipt, indent=2) + '\n')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    for name in ('source', 'registry', 'rust', 'git', 'wasi', 'runtimes', 'assets', 'destination'):
        parser.add_argument('--' + name, type=Path, required=True)
    parser.add_argument('--allow-ui-wall-clock', action='store_true')
    parser.add_argument('--allow-ui-monotonic-clock', action='store_true')
    parser.add_argument('--allow-ci-link-trace', action='store_true')
    parser.add_argument('--allow-inactive-ui-libc', action='store_true')
    run(**vars(parser.parse_args()))
