"""Bind exact built stack-probe inputs to the separate supported macOS test."""
import argparse
import json
from pathlib import Path

from setup_native_inputs import values


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    for name in ('artifact', 'oracle', 'evidence', 'github-env'):
        parser.add_argument('--' + name, type=Path, required=True)
    args = parser.parse_args()
    environment = {
        name.replace('TOBKIRI_TEST_SETUP_', 'TOBKIRI_TEST_INPUT_STACK_'): value
        for name, value in values(args.artifact, args.oracle, args.evidence).items()
    }
    environment['TOBKIRI_TEST_INPUT_STACK_CI'] = '1'
    with args.github_env.open('a') as stream:
        for name, value in environment.items():
            stream.write(f'{name}={value}\n')
    print(json.dumps({'stack_probe_inputs_bound': True, 'runtime_proof': False}))
