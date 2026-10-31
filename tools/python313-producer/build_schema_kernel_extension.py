"""Compile only the existing private kernel for the separate App Wasm trial.

This CI-only producer neither builds the generic64 probe nor invokes a guest.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

from build_native import GIB, fingerprint, sandbox_command
from build_wasi import run_stage
import build_rpds
import build_schema_kernel as kernel
import fetch_sdk
import runtime_inputs


def run(source: Path, vendor: Path, archives: Path, rust: Path,
        sdk: Path, destination: Path, app_backend: bool = False) -> None:
    """Verify the original compiler/source closure and compile offline once."""
    if type(app_backend) is not bool or sys.platform != 'linux' or os.environ.get('GITHUB_ACTIONS') != 'true':
        raise ValueError('kernel extension compilation requires Linux CI')
    for path in (source, vendor, archives, rust, sdk, destination.parent):
        build_rpds.physical(path)
    if os.path.lexists(destination):
        raise ValueError('new extension output required')
    kernel.packages(source)
    source_files = fetch_sdk.inventory(source)
    if (set(source_files) != {'Cargo.toml', 'Cargo.lock', 'README.md',
                              'COPYING.jsonschema', 'adapter.py', 'src/lib.rs',
                              'src/traversal.rs', 'src/keywords.rs', 'src/app_schema.rs', 'app_provider.py'}
            or any('link' in row for row in source_files.values())
            or sum(row['bytes'] for row in source_files.values()) > kernel.MAX_SOURCE):
        raise ValueError('closed original kernel source required')
    registry = kernel.verify_registry(source, vendor, archives)
    build_rpds.verify_rust(rust, runtime_inputs.report(rust/'rust-manifest.json'))
    sdk_manifest = sdk/'sdk-manifest.json'
    sdk_report = json.loads(kernel.fingerprint_and_read(sdk_manifest, 16*1024*1024))
    build_rpds.verify_sdk(sdk, sdk_report)
    destination.mkdir(mode=0o700)
    work = destination/'work'
    work.mkdir(mode=0o700)
    (work/'cargo-home').mkdir(mode=0o700)
    config = destination/'build-config'
    config.mkdir(mode=0o700)
    (config/'pyo3-config.txt').write_text(build_rpds.PYO3_CONFIG)
    (config/'cargo.toml').write_text(build_rpds.CARGO_CONFIG)
    recipe = destination/'build.sh'
    code = build_rpds.RECIPE.replace('cd /rpds\n', 'cd /kernel\n')
    if app_backend:
        anchor = '--features pyo3/abi3-py313'
        if code.count(anchor) != 1:
            raise ValueError('exact compiler feature anchor required')
        code = code.replace(anchor, anchor+',app-schema-backend')
    recipe.write_text(code)
    fixed = {str(path): fingerprint(path) for path in (
        recipe, config/'pyo3-config.txt', config/'cargo.toml')}
    report = {'format': 'tobkiri.app-schema-kernel-extension/1',
              'source_head': os.environ.get('GITHUB_SHA'), 'source_files': source_files,
              'registry': registry, 'outcome': 'failure', 'guest_executed': False,
              'production_enabled': False, 'configuration': fixed,
              'features': ['app-schema-backend'] if app_backend else []}
    try:
        command = sandbox_command(None, work, recipe, (
            (source, '/kernel'), (vendor, '/vendor'), (rust/'toolchain', '/rust'),
            (sdk/'sdk', '/sdk'), (config, '/build-config')), address_space_bytes=4*GIB)
        report['supervision'] = run_stage('app-kernel-wasm-extension', command,
                                          work, destination/'build')
        messages = [json.loads(line) for line in kernel.fingerprint_and_read(
            work/'cargo-artifacts.jsonl', 4*1024*1024).splitlines()]
        artifacts = [row for row in messages if row.get('reason') == 'compiler-artifact'
                     and row.get('manifest_path') == '/kernel/Cargo.toml'
                     and row.get('target', {}).get('name') == kernel.EXTENSION
                     and row['target'].get('kind') == ['cdylib']]
        if (len(artifacts) != 1 or len(artifacts[0].get('filenames', [])) != 1
                or [row.get('success') for row in messages
                    if row.get('reason') == 'build-finished'] != [True]):
            raise ValueError('one successful actual kernel cdylib required')
        features = artifacts[0].get('features', [])
        if set(features) != ({'default', 'app-schema-backend'} if app_backend else {'default'}):
            raise ValueError('actual Wasm compiler features differ')
        report['compiled_features'] = features
        name = artifacts[0]['filenames'][0]
        if name != '/work/target/wasm32-wasip2/release/_tobkiri_schema_kernel.wasm':
            raise ValueError('kernel escaped its actual target')
        artifact = work/'target/wasm32-wasip2/release/_tobkiri_schema_kernel.wasm'
        identity = fingerprint(artifact, 4*1024*1024)
        if artifact.read_bytes()[:8] != b'\x00asm\x01\x00\x00\x00':
            raise ValueError('actual core Wasm extension required')
        limited = list((work/'target/wasm32-wasip2/release/build').glob('pyo3-*/output'))
        if not any('cargo:rustc-cfg=Py_LIMITED_API' in path.read_text() for path in limited):
            raise ValueError('actual limited API compilation required')
        if (fetch_sdk.inventory(source) != source_files
                or kernel.verify_registry(source, vendor, archives) != registry
                or any(fingerprint(Path(path)) != pin for path, pin in fixed.items())):
            raise ValueError('compiler inputs changed')
        build_rpds.verify_rust(rust, runtime_inputs.report(rust/'rust-manifest.json'))
        build_rpds.verify_sdk(sdk, json.loads(
            kernel.fingerprint_and_read(sdk_manifest, 16*1024*1024)))
        report.update(outcome='success', artifact=identity,
                      artifact_path=artifact.relative_to(destination).as_posix())
    finally:
        (destination/'extension-build.json').write_text(json.dumps(report, indent=2)+'\n')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--app-backend', action='store_true')
    for name in ('source', 'vendor', 'archives', 'rust', 'sdk', 'destination'):
        parser.add_argument('--'+name, type=Path, required=True)
    run(**vars(parser.parse_args()))
