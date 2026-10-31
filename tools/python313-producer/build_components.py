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
import capability_projection_inputs
import bundle_catalog_inputs
from fetch_sdk import inventory
import fetch_validation_tools
import harness_native_dependencies
import runtime_inputs


def run(source: Path | None, producer: Path, validation: Path, destination: Path,
        *, all_five: bool = False, harness_setup: bool = False,
        harness_native: bool = False,
        native_input_stack: bool = False,
        native_input_stack_import_diagnostics: bool = False,
        native_thread_capabilities: bool = False,
        capability_projection: bool = False,
        bundle_catalog: bool = False,
        bundle_catalog_kernel: bool = False,
        app_schema_backend: bool = False,
        kernel_extension: Path | None = None,
        bundle_cases: Path | None = None,
        schema_inputs: Path | None = None, stdlib: Path | None = None,
        rpds: Path | None = None) -> None:
    """Build each exact original Pack separately with no production activation."""
    if sys.platform != 'linux' or os.environ.get('GITHUB_ACTIONS') != 'true':
        raise ValueError('original component rebuilding is restricted to Linux CI')
    if sum((
            all_five,
            harness_setup,
            harness_native,
            native_input_stack,
            native_input_stack_import_diagnostics,
            native_thread_capabilities,
            capability_projection,
            bundle_catalog,
            bundle_catalog_kernel,
    )) > 1:
        raise ValueError('component build selectors are separate build scopes')
    if type(app_schema_backend) is not bool or (app_schema_backend and not bundle_catalog_kernel):
        raise ValueError('App backend is a distinct explicitly selected kernel scope')
    app_data_mode = capability_projection or bundle_catalog or bundle_catalog_kernel
    if (bundle_catalog or bundle_catalog_kernel) != (bundle_cases is not None):
        raise ValueError('bundle catalog requires its separate original DATA input')
    if bundle_catalog_kernel != (kernel_extension is not None):
        raise ValueError('separate kernel trial requires its actual extension build')
    if app_data_mode:
        if any(path is None for path in (schema_inputs, stdlib, rpds)):
            raise ValueError('App DATA component requires all locked schema inputs')
        dependency_mode = True
    else:
        dependency_mode = harness_native_dependencies.selected(
            harness_native, schema_inputs, stdlib, rpds,
        )
    physical_inputs = (producer, validation) + (
        (schema_inputs, stdlib, rpds) if dependency_mode else ()
    ) + ((bundle_cases,) if bundle_cases is not None else ()) + (
        (kernel_extension,) if kernel_extension is not None else ())
    if (not destination.is_absolute() or not destination.parent.is_dir()
            or destination.parent.resolve() != destination.parent
            or any(not p.is_absolute() or p.resolve() != p for p in physical_inputs)
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
    dependency_inputs = (
        harness_native_dependencies.verify(
            schema_inputs, producer, rpds, validation, stdlib,
        ) if dependency_mode else None
    )
    repo = HERE.parents[1]
    sdk = repo / 'sdk/python'
    snapshots = {str(sdk): inventory(sdk), str(validation): inventory(validation)}
    identities = {str(p): fingerprint(p) for p in (
        binary, producer_receipt_path, source_receipt, tool_receipt_path)}
    driver = HERE / 'component_driver.py'
    identities[str(driver)] = fingerprint(driver)
    if bundle_cases is not None:
        identities[str(bundle_cases)] = fingerprint(bundle_cases, 4 * 1024 * 1024)
    if kernel_extension is not None:
        for path in (kernel_extension / 'extension-build.json',
                     kernel_extension / 'work/target/wasm32-wasip2/release/_tobkiri_schema_kernel.wasm'):
            identities[str(path)] = fingerprint(path, 4 * 1024 * 1024)
        snapshots[str(HERE / 'schema_kernel')] = inventory(HERE / 'schema_kernel')
    empty = destination / 'empty-venv'
    empty.mkdir(mode=0o700)
    recipe = destination / 'componentize.sh'
    recipe.write_text('set -eu\n/usr/bin/python3 -I -B /driver.py\n')
    identities[str(recipe)] = fingerprint(recipe)
    if bundle_catalog_kernel:
        receipt_format = ('tobkiri.app-frame-bundle-catalog-native-candidate/1' if app_schema_backend
                          else 'tobkiri.rust-bundle-catalog-native-candidate/1')
    elif bundle_catalog:
        receipt_format = 'tobkiri.python313-bundle-catalog-candidates/1'
    elif capability_projection:
        receipt_format = 'tobkiri.python313-capability-projection-candidates/1'
    elif native_thread_capabilities:
        receipt_format = (
            'tobkiri.python313-native-thread-capabilities-diagnostic-candidate/1'
        )
    elif native_input_stack_import_diagnostics:
        receipt_format = (
            'tobkiri.python313-native-input-stack-import-diagnostics-candidate/1'
        )
    elif native_input_stack:
        receipt_format = 'tobkiri.python313-native-input-stack-candidate/1'
    elif harness_native:
        receipt_format = 'tobkiri.python313-native-harness-candidate/1'
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
    if app_data_mode:
        receipt.update(
            same_app_adapter_two_worlds=not bundle_catalog_kernel,
            native_scope='pure DATA comparison only; no authority or product parity',
            ordinary_pack_connected=False,
        )
    elif native_thread_capabilities:
        receipt.update(
            diagnostic_only=True,
            diagnostic_scope='CPython thread/context/termination capability probe',
            whole_component_parity=False,
            product_resident_concurrency_cancel_acceptance_claimed=False,
        )
    elif native_input_stack_import_diagnostics:
        receipt['diagnostic_only'] = True
        receipt['diagnostic_outcome_must_fail'] = True
    receipt_path = destination / 'components-build.json'
    if bundle_catalog_kernel:
        from bundle_catalog_kernel_inputs import COMPONENT, APP_COMPONENT
        component_names = (APP_COMPONENT if app_schema_backend else COMPONENT,)
    elif bundle_catalog:
        component_names = ('harness-bundle-catalog-native', 'harness-bundle-catalog',
                           'harness-bundle-catalog-reread-native')
    elif capability_projection:
        component_names = ('harness-capability-projection-native', 'harness-capability-projection')
    elif native_thread_capabilities:
        component_names = ('harness-thread-capabilities',)
    elif native_input_stack_import_diagnostics:
        component_names = ('harness-input-stack-import-diagnostics',)
    elif native_input_stack:
        component_names = ('harness-input-stack',)
    elif harness_native:
        component_names = ('harness-native',)
    elif harness_setup:
        component_names = ('harness-setup',)
    else:
        component_names = component_inputs.plans(all_five)
    try:
        for name in component_names:
            output = destination / name
            output.mkdir(mode=0o700)
            inputs = output / 'inputs'
            projection_native = name == 'harness-capability-projection-native'
            bundle_native = name in ('harness-bundle-catalog-native',
                                    'harness-bundle-catalog-reread-native') or bundle_catalog_kernel
            bundle_diagnostic = name == 'harness-bundle-catalog-reread-native'
            plan_receipt = (
                bundle_catalog_inputs.stage(
                    repo, inputs, dependency_inputs, schema_inputs, stdlib, rpds,
                    native=bundle_native, bundle_cases=bundle_cases,
                    diagnostic_reread=bundle_diagnostic,
                    kernel_extension=kernel_extension, app_schema_backend=app_schema_backend,
                )
                if bundle_catalog or bundle_catalog_kernel
                else capability_projection_inputs.stage(
                    repo, inputs, dependency_inputs, schema_inputs, stdlib, rpds,
                    native=projection_native,
                )
                if capability_projection
                else component_inputs.stage_native_thread_capabilities(repo, inputs)
                if native_thread_capabilities
                else component_inputs.stage_native_input_stack_import_diagnostics(
                    repo, inputs
                )
                if native_input_stack_import_diagnostics
                else component_inputs.stage_native_input_stack(repo, inputs)
                if native_input_stack
                else component_inputs.stage_harness_native(repo, inputs)
                if harness_native
                else component_inputs.stage_harness_setup(repo, inputs)
                if harness_setup
                else component_inputs.stage(repo, name, inputs)
            )
            if dependency_mode and not app_data_mode:
                plan_receipt = harness_native_dependencies.stage(
                    inputs, plan_receipt, dependency_inputs,
                    schema_inputs, stdlib, rpds,
                )
            snapshots[str(inputs)] = plan_receipt['files']
            control = output / 'control.json'
            control_data = {
                'producer_sha256': producer_report['binary']['sha256'],
                'producer_receipt_sha256': identities[str(producer_receipt_path)]['sha256'],
                'plan_sha256': plan_receipt['plan_sha256'],
                'retain_generated_bindings': (
                    native_input_stack or native_input_stack_import_diagnostics
                    or native_thread_capabilities or harness_native
                    or app_data_mode
                ),
            }
            if native_thread_capabilities:
                control_data['native_thread_capabilities'] = True
            if dependency_mode and not app_data_mode:
                control_data['harness_native_dependencies'] = plan_receipt[
                    'harness_native_dependencies'
                ]
            control.write_text(json.dumps(control_data) + '\n')
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
            if bundle_catalog or bundle_catalog_kernel:
                bundle_catalog_inputs.verify_component(
                    proof, plan_receipt, native=bundle_native,
                    diagnostic_reread=bundle_diagnostic,
                    kernel_derived=bundle_catalog_kernel, app_schema_backend=app_schema_backend,
                )
            elif capability_projection:
                capability_projection_inputs.verify_component(
                    proof, plan_receipt, native=projection_native,
                )
            elif dependency_mode:
                harness_native_dependencies.verify_component_receipt(
                    proof, plan_receipt, inputs,
                )
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
        if dependency_mode and harness_native_dependencies.verify(
            schema_inputs, producer, rpds, validation, stdlib,
        ) != dependency_inputs:
            raise ValueError('native Harness dependency inputs changed')
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
    mode.add_argument('--harness-native', action='store_true')
    mode.add_argument('--native-input-stack', action='store_true')
    mode.add_argument('--native-input-stack-import-diagnostics', action='store_true')
    mode.add_argument('--native-thread-capabilities', action='store_true')
    mode.add_argument('--capability-projection', action='store_true')
    mode.add_argument('--bundle-catalog', action='store_true')
    mode.add_argument('--bundle-catalog-kernel', action='store_true')
    parser.add_argument('--app-schema-backend', action='store_true')
    parser.add_argument('--kernel-extension', type=Path)
    parser.add_argument('--bundle-cases', type=Path)
    for name in ('schema-inputs', 'stdlib', 'rpds'):
        parser.add_argument('--' + name, type=Path)
    for name in ('producer', 'validation', 'destination'):
        parser.add_argument('--' + name, type=Path, required=True)
    run(**vars(parser.parse_args()))
