"""Replay the actual Setup component in a separately supervised Linux sandbox.

All Core imports trap; there is no Host authority, native Core or GUI substitute.
Compilation is covered by the outer process supervisor, not guest fuel/epochs.
"""

from __future__ import annotations

import argparse
import hashlib
from importlib.metadata import version
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))
import prepare_harness_setup as prepare
from replay_ordered_tool_bridge_v2 import (
    DEFAULT_FUEL, STORE_LIMITS, _EpochTimer, _export, _invoke,
)

sys.path.insert(0, str(prepare.ROOT / 'tobkiri_runtime'))
from ecosystem.defaultspack.defaultspack.profile_runtime_composition import (
    DefaultspackProfileRuntime,
)
from ordered_json_v2 import dumps

LIFECYCLE = 'tobkiri:core/lifecycle@1.0.0'


def source_cases() -> list[dict]:
    """Compute expected bytes through the complete original application module.

    The candidate adapter/extracted module is never the behavioral oracle.
    Arbitrary accepted confirmation/receipt fields remain recorded DATA only.
    """
    runtime = DefaultspackProfileRuntime()
    confirmation = {'profile_id': 'defaults', 'security_epoch': 4,
                    'plan_digest': 'recorded', 'bindings': [{'approved': True}]}
    listing = {'recommended_default_profile': {'confirmation': confirmation}}
    body = {'setup_api_version': 'io.tobkiri.setup-state.v4',
            'operation_id': 'defaults.activate', 'confirmed': True,
            'confirmation': confirmation}
    inputs = [
        ('exact', body, listing),
        ('reordered', {**body, 'confirmation': dict(reversed(list(confirmation.items())))}, listing),
        ('stale', {**body, 'confirmation': {**confirmation, 'security_epoch': 3}}, listing),
        ('false', {**body, 'confirmed': False}, listing),
        ('integer-one', {**body, 'confirmed': 1}, listing),
        ('extra-approved', {**body, 'approved': True}, listing),
        ('missing-key', {key: value for key, value in body.items() if key != 'confirmed'}, listing),
        ('renamed-operation', {**body, 'operation_id': 'harness.activate'}, listing),
        ('malformed-confirmation', {**body, 'confirmation': []}, listing),
        ('missing-preview', body, {}),
        ('host-preparation', body, None),
        ('host-preparation-malformed', {**body, 'confirmation': 'invalid'}, None),
    ]
    cases = []
    for name, request, state in inputs:
        decision = runtime.setup_activation_decision(request, state)
        cases.append({'id': name, 'operation': 'decision',
                      'input': dumps({'body': request, 'listing': state}),
                      'expected': dumps({'confirmation': decision.confirmation,
                                         'response': decision.response})})
    facts = {'profile': {'profile_id': 'defaults'},
             'plan': {'profile_revision': 'revision', 'plan_digest': 'plan'},
             'activation': {'activation_id': 'activation', 'security_epoch': 4,
                            'fencing_token': 'fence',
                            'profile_authority_snapshot_digest': 'authority'},
             'audit_receipt': {'status': 'recorded'}}
    active = SimpleNamespace(resolved=SimpleNamespace(
        profile=facts['profile'], plan=facts['plan']), activation=facts['activation'])
    cases.append({'id': 'intermediate-success', 'operation': 'success',
                  'input': dumps(facts),
                  'expected': dumps(runtime.setup_activation_success(active, facts['audit_receipt']))})
    cases.append({'id': 'failure', 'operation': 'failure', 'input': '{}',
                  'expected': dumps(runtime.setup_activation_failure())})
    for name, route in (('retired', None), ('retired-route', '/api/setup/packs/install')):
        cases.append({'id': name, 'operation': 'retired',
                      'input': dumps({'route': route}),
                      'expected': dumps(runtime.retired_setup_response(route=route))})
    for name, payload in (('invalid-shape', '[]'), ('duplicate-key', '{"body":{},"body":{}}')):
        cases.append({'id': name, 'operation': 'decision', 'input': payload,
                      'expected_error': {'code': 'invalid', 'message': 'invalid Setup projection input'}})
    return cases


def observe(engine, component, linker, case: dict) -> dict:
    """Require actual returned calls, post-return cleanup and ordinary shutdown."""
    from wasmtime import Store
    from wasmtime.component import Record
    result = {'id': case['id'], 'passed': False, 'calls': [],
              'init': False, 'shutdown': False, 'failures': []}
    timer = None
    try:
        with Store(engine) as store:
            store.set_limits(**STORE_LIMITS)
            store.set_fuel(DEFAULT_FUEL)
            store.set_epoch_deadline(1)
            timer = _EpochTimer(30, engine.increment_epoch)
            timer.start()
            cleanup_failures = []
            shutdown = None

            def call(name, function, *arguments):
                def returned(value):
                    result['calls'].append(name)
                return _invoke(function, store, *arguments, on_return=returned,
                               cleanup_failures=cleanup_failures)

            try:
                instance = linker.instantiate(store, component)
                init = _export(instance, store, LIFECYCLE, 'init')
                shutdown = _export(instance, store, LIFECYCLE, 'shutdown')
                function = _export(instance, store, prepare.INTERFACE, case['operation'])
                if call('init', init, '{}') is not None:
                    raise ValueError('initialization did not succeed')
                result['init'] = True
                value = call(case['operation'], function, case['input'])
                if 'expected_error' in case:
                    if not isinstance(value, Record) or set(vars(value)) != {'code', 'message'}:
                        raise ValueError('typed Core error expected')
                    result['matched'] = vars(value) == case['expected_error']
                else:
                    result['matched'] = type(value) is str and value == case['expected']
            except Exception as error:
                result['failures'].append(type(error).__name__)
            finally:
                if shutdown is not None and result['init']:
                    try:
                        if call('shutdown', shutdown) is not None:
                            raise ValueError('shutdown did not succeed')
                        result['shutdown'] = True
                    except Exception as error:
                        result['failures'].append(type(error).__name__)
                result['failures'].extend(type(error).__name__ for error in cleanup_failures)
                result['fuel_consumed'] = DEFAULT_FUEL - store.get_fuel()
                finishing, timer = timer, None
                finishing.finish()
    except Exception as error:
        result['failures'].append(type(error).__name__)
    finally:
        if timer is not None:
            try:
                timer.finish()
            except Exception as error:
                result['failures'].append(type(error).__name__)
    result['passed'] = (result.get('matched') is True and not result['failures']
                        and result['init'] and result['shutdown']
                        and result['calls'] == ['init', case['operation'], 'shutdown'])
    return result


def run(artifact: Path, output: Path) -> None:
    """Validate current source/receipt, then execute only the exact typed guest."""
    if sys.platform != 'linux' or version('wasmtime') != '48.0.0':
        raise ValueError('pinned Linux CI validation runtime required')
    from wasmtime import Config, Engine
    from wasmtime.component import Component, Linker
    binary = prepare.bounded_read(artifact, 32 * 1024 * 1024)
    receipt = json.loads(prepare.bounded_read(artifact.with_suffix('.build.json'), 256 * 1024))
    with tempfile.TemporaryDirectory(dir=output.parent) as temporary:
        staged = Path(temporary) / 'source'
        plan = prepare.stage(staged)
        manifest = [{**item, 'bytes': len(prepare.bounded_read(staged / item['source']))}
                    for item in plan['sources']]
        if (receipt['format'] != 'tobkiri.component-build/2'
                or receipt['artifact_sha256'] != hashlib.sha256(binary).hexdigest()
                or receipt['artifact_bytes'] != len(binary)
                or receipt['build_plan_sha256'] != hashlib.sha256((staged / 'plan.json').read_bytes()).hexdigest()
                or receipt['source_manifest'] != manifest
                or receipt['imports'] != sorted(plan['allowed_imports'])
                or receipt['exports'] != sorted(plan['outer_exports'])):
            raise ValueError('component differs from complete current source plan')
    cases = source_cases()
    config = Config()
    config.parallel_compilation = False
    config.consume_fuel = config.epoch_interruption = True
    engine = Engine(config)
    component = Component(engine, binary)
    if (set(component.type.imports(engine)) != set(plan['allowed_imports'])
            or set(component.type.exports(engine)) != set(plan['outer_exports'])):
        raise ValueError('actual component differs from the typed source boundary')
    linker = Linker(engine)
    linker.define_unknown_imports_as_traps(component)
    observations = [observe(engine, component, linker, case) for case in cases]
    passed = (len(observations) == 18 and all(item['passed'] for item in observations)
              and [item['id'] for item in observations] == [case['id'] for case in cases])
    report = {'format': 'tobkiri.harness-setup-standalone-replay/1',
              'passed': passed, 'component_sha256': hashlib.sha256(binary).hexdigest(),
              'cases': observations, 'expected_cases': len(cases),
              'source_case_sha256': hashlib.sha256(dumps(cases).encode()).hexdigest(),
              'all_core_imports_trap': True, 'native_core_replay': False,
              'guest_runtime_identity_verified': False,
              'full_setup_gui_tested': False, 'production_enabled': False}
    with output.open('x') as stream:
        json.dump(report, stream, indent=2)
        stream.write('\n')
    if not passed:
        raise ValueError('actual Setup guest comparison or lifecycle failed')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--artifact', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    # The controller mounts this exact pinned wheel only inside its namespace.
    sys.path.insert(0, '/validation-deps')
    run(args.artifact, args.output)
