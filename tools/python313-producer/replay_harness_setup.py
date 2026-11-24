"""Supervise an actual Setup Wasm replay on the CI host, never on the Mac."""

import argparse
import json
import os
from pathlib import Path
import sys

from build_native import GIB, HERE, fingerprint, sandbox_command
from build_wasi import run_stage
import fetch_validation_tools
import runtime_inputs


def run(artifact: Path, validation: Path, destination: Path) -> None:
    """Bind the candidate and validation tools before isolated guest execution."""
    if sys.platform != 'linux' or os.environ.get('GITHUB_ACTIONS') != 'true':
        raise ValueError('actual Setup replay is restricted to Linux CI')
    if any(not path.is_absolute() or path.resolve() != path
           for path in (artifact, validation, destination.parent)):
        raise ValueError('physical absolute inputs and existing output parent required')
    pins = {str(path): fingerprint(path, 32 * 1024 * 1024) for path in
            (artifact, artifact.with_suffix('.build.json'))}
    tool_report = runtime_inputs.report(validation / 'validation-inputs.json')
    if tool_report['inputs'] != list(fetch_validation_tools.INPUTS):
        raise ValueError('validation tool pins changed')
    runtime_inputs.recorded_files(validation / 'wasmtime', tool_report['files'])
    destination.mkdir(mode=0o700)
    work = destination / 'work'
    work.mkdir(mode=0o700)
    recipe = destination / 'replay.sh'
    recipe.write_text('set -eu\n/usr/bin/python3 -I -B '
                      '/replay-source/scripts/wasm/replay_harness_setup.py '
                      '--artifact /artifact/component.wasm --output /work/replay.json\n')
    repo = HERE.parents[1]
    pins[str(recipe)] = fingerprint(recipe)
    command = sandbox_command(None, work, recipe, (
        (repo, '/replay-source'), (artifact, '/artifact/component.wasm'),
        (artifact.with_suffix('.build.json'), '/artifact/component.build.json'),
        (validation / 'wasmtime', '/validation-deps'),
    ), address_space_bytes=8 * GIB)
    supervision = run_stage('harness-setup-replay', command, work, destination / 'supervision')
    report = runtime_inputs.report(work / 'replay.json')
    if (report['format'] != 'tobkiri.harness-setup-standalone-replay/1'
            or report['passed'] is not True or report['expected_cases'] != 30
            or report['native_core_replay'] is not False
            or report['production_enabled'] is not False):
        raise ValueError('Setup guest replay did not complete its declared scope')
    for path, expected in pins.items():
        if fingerprint(Path(path), 32 * 1024 * 1024) != expected:
            raise ValueError('Setup replay input changed')
    runtime_inputs.recorded_files(validation / 'wasmtime', tool_report['files'])
    (destination / 'replay-receipt.json').write_text(json.dumps({
        'format': 'tobkiri.harness-setup-supervised-replay/1',
        'supervision': supervision, 'inputs_unchanged': True, 'inputs': pins,
        'report': fingerprint(work / 'replay.json'),
        'native_core_replay': False, 'full_setup_gui_tested': False,
        'production_enabled': False,
    }, indent=2) + '\n')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    for name in ('artifact', 'validation', 'destination'):
        parser.add_argument('--' + name, type=Path, required=True)
    run(**vars(parser.parse_args()))
