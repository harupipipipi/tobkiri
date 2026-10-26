"""Restore the three missing original Python components on the isolated CI host."""
import argparse
import json
import os
from pathlib import Path
import sys

from build_native import GIB, HERE, STAGED_SHA256, fingerprint, sandbox_command, verify_tree
from build_cli import RECIPE_SHA256 as CLI_RECIPE_SHA256
from build_wasi import run_stage
import component_inputs
from fetch_sdk import inventory
import fetch_validation_tools
import runtime_inputs


def run(source: Path | None, producer: Path, validation: Path, destination: Path,
        *, all_five: bool = False, harness_setup: bool = False,
        native_input_stack: bool = False,
        native_input_stack_import_diagnostics: bool = False) -> None:
    """Build each exact original Pack separately with no production activation."""
    if sys.platform != 'linux' or os.environ.get('GITHUB_ACTIONS') != 'true':
        raise ValueError('original component rebuilding is restricted to Linux CI')
    if sum((
            all_five,
            harness_setup,
            native_input_stack,
            native_input_stack_import_diagnostics,
    )) > 1:
        raise ValueError('component build selectors are separate build scopes')
    if (not destination.is_absolute() or not destination.parent.is_dir()
            or destination.parent.resolve() != destination.parent
            or any(not p.is_absolute() or p.resolve() != p for p in (producer, validation))
            or (source is not None and (not source.is_absolute() or source.resolve() != source))):
        raise ValueError('physical build inputs and new output directory required')
    destination.mkdir(mode=0o700)
    producer_receipt_path = producer / 'cli-build.json'
    producer_report = runtime_inputs.report(producer_receipt_path)
    source_identity = producer_report['inputs']['source']
    if (producer_report['format'] != 'tobkiri.python313-cli-rebuild/1'
            or producer_report['original_recipe_sha256'] != CLI_RECIPE_SHA256
            or source_identity['sha256'] != STAGED_SHA256
            or producer_report['reported_version'] != 'componentize-py 0.25.0'):
        raise ValueError('producer provenance differs from original reviewed recipe')
    if source is not None and verify_tree(source) != source_identity:
        raise ValueError('original producer source differs from its build receipt')
    source_receipt = producer / 'cli-source.json'
    source_pin = [value for path, value in producer_report['predecessors'].items()
                  if Path(path).name == 'cli-source.json']
    if len(source_pin) != 1 or fingerprint(source_receipt, 4 * 1024 * 1024) != source_pin[0]:
        raise ValueError('producer publishing-source derivation receipt changed')
    binary = producer / 'work/target/release/componentize-py'
    if (not producer_report['inputs_unchanged'] or not producer_report['producer_cli']
            or fingerprint(binary, 128 * 1024 * 1024) != producer_report['binary']):
        raise ValueError('original producer differs from successful build receipt')
    tool_receipt_path = validation / 'validation-inputs.json'
    tool_report = runtime_inputs.report(tool_receipt_path)
    if tool_report['inputs'] != list(fetch_validation_tools.INPUTS):
        raise ValueError('validation tools differ from pinned inputs')
    for pin in fetch_validation_tools.INPUTS:
        if fingerprint(validation / pin['filename'], 100 * 1024 * 1024) != {
                k: pin[k] for k in ('bytes', 'sha256')}:
            raise ValueError('actual validation tool input changed')
    runtime_inputs.recorded_files(validation / 'wasmtime', tool_report['files'])
    repo = HERE.parents[1]
    sdk = repo / 'sdk/python'
    snapshots = {str(sdk): inventory(sdk), str(validation): inventory(validation)}
    identities = {str(p): fingerprint(p) for p in (
        binary, producer_receipt_path, source_receipt, tool_receipt_path)}
    driver = HERE / 'component_driver.py'
    identities[str(driver)] = fingerprint(driver)
    empty = destination / 'empty-venv'
    empty.mkdir(mode=0o700)
    recipe = destination / 'componentize.sh'
    recipe.write_text('set -eu\n/usr/bin/python3 -I -B /driver.py\n')
    identities[str(recipe)] = fingerprint(recipe)
    if native_input_stack_import_diagnostics:
        receipt_format = (
            'tobkiri.python313-native-input-stack-import-diagnostics-candidate/1'
        )
    elif native_input_stack:
        receipt_format = 'tobkiri.python313-native-input-stack-candidate/1'
    elif harness_setup:
        receipt_format = 'tobkiri.python313-setup-candidate/1'
    else:
        receipt_format = 'tobkiri.python313-original-components/1'
    receipt = {'format': receipt_format, 'outcome': 'pending',
               'source': source_identity, 'producer': producer_report['binary'],
               'producer_source_verification': ('actual original source tree and successful receipt'
                   if source is not None else 'successful producer and publishing-source receipts; source tree not restored'),
               'all_five_original_components': all_five,
               'producer_receipt': identities[str(producer_receipt_path)],
               'validation_receipt': identities[str(tool_receipt_path)],
               'driver': identities[str(driver)], 'components': {},
               'guest_runtime_identity_verified': False, 'native_core_replay': False,
               'production_enabled': False, 'historical_binary_identity_claimed': False}
    if native_input_stack_import_diagnostics:
        receipt['diagnostic_only'] = True
        receipt['diagnostic_outcome_must_fail'] = True
    receipt_path = destination / 'components-build.json'
    if native_input_stack_import_diagnostics:
        component_names = ('harness-input-stack-import-diagnostics',)
    elif native_input_stack:
        component_names = ('harness-input-stack',)
    elif harness_setup:
        component_names = ('harness-setup',)
    else:
        component_names = component_inputs.plans(all_five)
    try:
        for name in component_names:
            output = destination / name
            output.mkdir(mode=0o700)
            inputs = output / 'inputs'
            plan_receipt = (
                component_inputs.stage_native_input_stack_import_diagnostics(
                    repo, inputs
                )
                if native_input_stack_import_diagnostics
                else component_inputs.stage_native_input_stack(repo, inputs)
                if native_input_stack
                else component_inputs.stage_harness_setup(repo, inputs)
                if harness_setup
                else component_inputs.stage(repo, name, inputs)
            )
            snapshots[str(inputs)] = plan_receipt['files']
            control = output / 'control.json'
            control.write_text(json.dumps({
                'producer_sha256': producer_report['binary']['sha256'],
                'producer_receipt_sha256': identities[str(producer_receipt_path)]['sha256'],
                'plan_sha256': plan_receipt['plan_sha256'],
                'retain_generated_bindings': (
                    native_input_stack or native_input_stack_import_diagnostics
                )}) + '\n')
            identities[str(control)] = fingerprint(control)
            work = output / 'work'
            work.mkdir(mode=0o700)
            mounts = ((binary, '/producer'), (validation / 'wac-cli', '/wac'),
                      (validation / 'wasmtime', '/validation-deps'), (sdk, '/sdk-python'),
                      (driver, '/driver.py'), (inputs, '/inputs'),
                      (control, '/control.json'), (empty, '/empty-venv'))
            # Historical component trials used the same 8 GiB producer process
            # limit. This is never a change to the Core or guest memory budget.
            command = sandbox_command(source, work, recipe, mounts, address_space_bytes=8 * GIB)
            supervision = run_stage(name, command, work, output / 'build')
            proof_path = work / 'component.build.json'
            fingerprint(proof_path, 4 * 1024 * 1024)
            proof = json.loads(proof_path.read_bytes())
            artifact = fingerprint(work / 'component.wasm', 32 * 1024 * 1024)
            if (proof['artifact_sha256'] != artifact['sha256']
                    or proof['artifact_bytes'] != artifact['bytes']
                    or proof['build_plan_sha256'] != plan_receipt['plan_sha256']
                    or proof['compiler_executable_sha256'] != producer_report['binary']['sha256']):
                raise ValueError('component output differs from its source and producer receipt')
            receipt['components'][name] = {'artifact': artifact, 'supervision': supervision,
                'source': plan_receipt, 'build_receipt': fingerprint(proof_path)}
            receipt_path.write_text(json.dumps(receipt, indent=2) + '\n')
        if source is not None and verify_tree(source) != source_identity:
            raise ValueError('original producer source changed')
        for path, expected in snapshots.items():
            runtime_inputs.recorded_files(Path(path), expected)
        for path, expected in identities.items():
            if fingerprint(Path(path)) != expected:
                raise ValueError('component build input identity changed')
        receipt.update(outcome='success', inputs_unchanged=True)
    except BaseException:
        receipt['outcome'] = 'failed'
        raise
    finally:
        receipt_path.write_text(json.dumps(receipt, indent=2) + '\n')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--source', type=Path)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--all-five', action='store_true')
    mode.add_argument('--harness-setup', action='store_true')
    mode.add_argument('--native-input-stack', action='store_true')
    mode.add_argument('--native-input-stack-import-diagnostics', action='store_true')
    for name in ('producer', 'validation', 'destination'):
        parser.add_argument('--' + name, type=Path, required=True)
    run(**vars(parser.parse_args()))
