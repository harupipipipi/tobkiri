"""Bind already built Setup inputs for a separate native Core CI job."""

import argparse
import json
from pathlib import Path

from build_native import fingerprint


def values(artifact: Path, oracle: Path, evidence: Path) -> dict:
    """Export bounded physical input identities, without executing or approving."""
    if (not evidence.is_absolute() or evidence.exists() or evidence.is_symlink()
            or evidence.parent.resolve() != evidence.parent or not evidence.parent.is_dir()):
        raise ValueError('fresh physical evidence output required')
    output = {'TOBKIRI_TEST_SETUP_EVIDENCE': str(evidence)}
    for label, path, limit in (
        ('COMPONENT', artifact, 32 * 1024 * 1024),
        ('RECEIPT', artifact.with_suffix('.build.json'), 256 * 1024),
        ('ORACLE', oracle, 1024 * 1024),
    ):
        if not path.is_absolute() or path.resolve() != path:
            raise ValueError('physical absolute native input required')
        identity = fingerprint(path, limit)
        output[f'TOBKIRI_TEST_SETUP_{label}'] = str(path)
        output[f'TOBKIRI_TEST_SETUP_{label}_SHA256'] = identity['sha256']
    if any('\n' in value or '\r' in value for value in output.values()):
        raise ValueError('invalid native environment value')
    return output


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    for name in ('artifact', 'oracle', 'evidence', 'github-env'):
        parser.add_argument('--' + name, type=Path, required=True)
    args = parser.parse_args()
    environment = values(args.artifact, args.oracle, args.evidence)
    with args.github_env.open('a') as stream:
        for name, value in environment.items():
            stream.write(f'{name}={value}\n')
    print(json.dumps({'native_inputs_bound': True, 'native_core_replay': False}))
