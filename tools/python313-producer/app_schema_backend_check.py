"""Pinned-CI differential check of the new App-owned Rust frame evaluator."""
from __future__ import annotations
import hashlib
import importlib.util
import json
from pathlib import Path
import sys


def compare(args, receipt, package_paths, extension):
    from build_schema_kernel import fingerprint_and_read
    from schema_kernel_observation import first_difference, meta_nodes, observe
    import sysconfig
    config = sysconfig.get_paths()
    stdlib = [config['stdlib'], config['platstdlib'], sysconfig.get_config_var('DESTSHARED')]
    here = Path(__file__).resolve().parent
    sys.path[:] = list(dict.fromkeys([str(here), str(extension.parent), str(args.source),
        *(str(p) for p in package_paths), *(p for p in stdlib if isinstance(p, str))]))
    if any(name == 'jsonschema' or name.startswith('jsonschema.') for name in sys.modules):
        raise ValueError('App backend must precede first original library import')
    import _tobkiri_schema_kernel as kernel
    if (Path(kernel.__file__).resolve(strict=True) != extension
            or kernel.production_enabled is not False
            or not hasattr(kernel, 'app_check_schema')):
        raise ValueError('actual feature-enabled extension required')
    spec = importlib.util.spec_from_file_location('app_schema_provider', args.source/'app_provider.py')
    if spec is None or spec.loader is None:
        raise ValueError('actual provider source unavailable')
    provider_module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = provider_module
    spec.loader.exec_module(provider_module)
    if Path(sys.modules['app_schema_model'].__file__).resolve(strict=True) != args.source/'app_schema_model.py':
        raise ValueError('actual owned model source required')
    provider = provider_module.AppSchemaProvider
    if kernel.app_schema_entry_count() != 0 or kernel.app_schema_frame_count() != 0:
        raise ValueError('App validation ran before the actual corpus')
    reference = json.loads(fingerprint_and_read(args.reference, 4*1024*1024))
    if (reference.get('format') != 'tobkiri.schema-kernel-reference/1'
            or len(reference.get('cases', [])) != 64):
        raise ValueError('complete original64 source reference required')
    from jsonschema import exceptions
    known = meta_nodes(provider)
    actual = []
    profiler = None
    if args.profile_calls:
        import cProfile
        profiler = cProfile.Profile()
    receipt['stage'] = 'app-backend-comparison'
    receipt['reference_counts_before_64'] = kernel.app_reference_counts()
    for case in reference['cases']:
        schema = case['schema']
        try:
            if profiler is None:
                provider.check_schema(schema)
            else:
                profiler.runcall(provider.check_schema, schema)
        except exceptions.SchemaError as error:
            result = {'valid': False, 'error': observe(error, schema, known)}
        else:
            result = {'valid': True, 'error': None}
        actual.append({'name': case['name'], 'actual': result})
        difference = first_difference(case['expected'], result)
        if difference is not None:
            receipt['first_difference'] = {'case': case['name'], 'difference': difference,
                                          'actual': result, 'expected': case['expected']}
            raise ValueError('App-owned native definition check differs: '+case['name'])
    if kernel.app_schema_entry_count() != 64 or kernel.app_schema_frame_count() <= 64:
        raise ValueError('each actual schema must enter Rust frames')
    receipt['reference_counts_after_64'] = kernel.app_reference_counts()
    if (receipt['reference_counts_after_64']['native_pointers'] <= 0
            or receipt['reference_counts_after_64']['native_dynamic_anchors'] <= 0):
        raise ValueError('actual owned builtin fragment paths were not exercised')
    if profiler is not None:
        import io
        import pstats
        text = io.StringIO()
        statistics = pstats.Stats(profiler, stream=text)
        statistics.sort_stats('cumulative').print_stats(35)
        statistics.sort_stats('time').print_stats(35)
        report = text.getvalue()
        if len(report.encode()) > 24*1024:
            raise ValueError('bounded native profile exceeded its limit')
        calls = []
        for entry in profiler.getstats():
            code = entry.code
            if isinstance(code, str):
                continue
            if ('referencing' in code.co_filename or 'jsonschema' in code.co_filename
                    or code.co_qualname.startswith(('Resource.', 'Resolver.', 'Resolved.', 'Specification.'))):
                calls.append({'file': code.co_filename, 'line': code.co_firstlineno,
                              'function': code.co_qualname, 'calls': entry.callcount,
                              'recursive_calls': entry.reccallcount})
        calls.sort(key=lambda row: (-row['calls'], row['file'], row['line']))
        receipt['native_call_profile'] = {
            'scope': 'only64 actual App-owned check_schema calls; boundaries and observer excluded',
            'native_only': True, 'wasm_fuel_attribution': False, 'benchmark_claim': False,
            'top_cumulative_and_self': report, 'reference_library_counts': calls[:60],
        }
    receipt['boundary_checks'] = boundary_checks(provider, kernel)
    from app_schema_reference_checks import reference_state_checks
    receipt['reference_state_checks'] = reference_state_checks(provider, kernel)
    from app_schema_fragment_checks import fragment_checks
    receipt['fragment_checks'] = fragment_checks(provider, kernel)
    from app_schema_callout_checks import callout_checks, owned_model_checks
    receipt['default_callout_checks'] = callout_checks(provider, kernel)
    receipt['owned_model_checks'] = owned_model_checks(provider, kernel)
    receipt.update(outcome='success', app_backend_64_success=True,
                   app_backend_entries=kernel.app_schema_entry_count(),
                   app_backend_frames=kernel.app_schema_frame_count(),
                   document_backend='original-preselected-operation',
                   actual_cases_sha256=hashlib.sha256(json.dumps(actual,sort_keys=True).encode()).hexdigest())


def boundary_checks(provider, kernel):
    """Actual error graph, mutation, callback order and no-original-retry checks."""
    from jsonschema import Draft202012Validator as original, FormatChecker, exceptions
    from schema_kernel_observation import first_difference, meta_nodes, observe
    import urllib.request

    saved_meta = {selected: selected.META_SCHEMA for selected in (original, provider)}
    cases = {}

    def scenario(name, make):
        observations = []
        for selected in (original, provider):
            meta, actual, events, checker = make()
            selected.META_SCHEMA = meta
            known = meta_nodes(selected)
            try:
                selected.check_schema(actual, format_checker=checker)
            except exceptions.SchemaError as error:
                outcome = {'valid': False, 'error': observe(error, actual, known)}
            else:
                outcome = {'valid': True, 'error': None}
            observations.append({'outcome': outcome, 'events': events, 'actual': actual})
        difference = first_difference(*observations)
        if difference is not None:
            raise ValueError('App backend boundary differs '+name+': '+str(difference))
        cases[name] = True

    def plain(meta, value):
        return lambda: (meta, value, [], None)

    try:
        scenario('false-items-many', plain({'items': False}, [1, 2]))
        scenario('false-items-one-after-prefix', plain({'prefixItems': [True], 'items': False}, [1, 2]))
        scenario('false-additional-properties', plain({'additionalProperties': False}, {'extra': 1}))
        scenario('any-of-invalid-context', plain({'anyOf': [{'type': 'string'}, {'type': 'integer'}]}, None))

        def later_callback(first_rule):
            def make():
                events = []
                checker = FormatChecker(formats=[])
                checker.checks('later')(lambda actual: events.append('later') or True)
                return first_rule, 'bad', events, checker
            return make
        scenario('any-of-stops-valid-branch', later_callback({'anyOf': [True, {'format': 'later'}]}))
        scenario('first-error-stops-later-callback', later_callback({'allOf': [{'type': 'integer'}, {'format': 'later'}]}))

        def mutate_later_schema():
            meta = {'properties': {'first': {'format': 'mutate'}, 'second': {'type': 'string'}}}
            actual = {'first': 0, 'second': 0}
            events = []
            checker = FormatChecker(formats=[])
            def change(value):
                events.append({'same_first': value is actual['first']})
                meta['properties']['second']['type'] = 'integer'
                return True
            checker.checks('mutate')(change)
            return meta, actual, events, checker
        scenario('callback-mutates-live-later-schema', mutate_later_schema)

        def mutate_instance_failure():
            meta, actual, events = {'format': 'mutate'}, {'touch': []}, []
            checker = FormatChecker(formats=[])
            def change(value):
                events.append({'same_input': value is actual})
                value['touch'].append('observed')
                return False
            checker.checks('mutate')(change)
            return meta, actual, events, checker
        scenario('callback-mutation-and-error-references', mutate_instance_failure)

        # Genuine lookup/wrapping through the original registry with an explicit
        # denied retrieve boundary. No network access occurs in this fixture.
        def graph(error, seen=None):
            if error is None:
                return None
            seen = {} if seen is None else seen
            if id(error) in seen:
                return {'ref': seen[id(error)]}
            index = len(seen)
            seen[id(error)] = index
            return {'id': index, 'type': type(error).__module__+'.'+type(error).__qualname__,
                    'message': str(error), 'suppressed': error.__suppress_context__,
                    'cause': graph(error.__cause__, seen), 'context': graph(error.__context__, seen)}
        saved_open = urllib.request.urlopen
        graphs = []
        try:
            for selected in (original, provider):
                supplied = RuntimeError('controlled META retrieve refusal')
                calls = []
                def denied(request):
                    calls.append(request.full_url)
                    raise supplied
                urllib.request.urlopen = denied
                selected.META_SCHEMA = {'$id': 'urn:tobkiri:meta-fixture', '$ref': 'urn:tobkiri:missing-meta'}
                try:
                    selected.check_schema({})
                except exceptions._WrappedReferencingError as error:
                    graphs.append({'graph': graph(error), 'calls': calls,
                                   'same_cause_and_context': error.__cause__ is error.__context__})
                else:
                    raise ValueError('missing META resource accepted')
            if any(row['calls'] != ['urn:tobkiri:missing-meta'] for row in graphs):
                raise ValueError('actual controlled META retrieve callback was not reached')
            if first_difference(*graphs) is not None or not graphs[1]['same_cause_and_context']:
                raise ValueError('META lookup exception cause/context differs: '+str(graphs))
            cases['unresolved-meta-reference-cause-context'] = True
        finally:
            urllib.request.urlopen = saved_open

        for selected, meta in saved_meta.items():
            selected.META_SCHEMA = meta
        saved_init = original.__init__
        class ConstructorCalled(Exception):
            pass
        def forbid_constructor(*args, **kwargs):
            raise ConstructorCalled('original validator execution is forbidden in native definition checks')
        original.__init__ = forbid_constructor
        try:
            before = kernel.app_schema_entry_count()
            provider.check_schema({'type': 'string'})
            try:
                provider.check_schema({'type': 'unknown-native-type'})
            except exceptions.SchemaError:
                pass
            else:
                raise ValueError('invalid native definition accepted')
            if kernel.app_schema_entry_count() != before+2:
                raise ValueError('native definition checks bypassed Rust')
            try:
                provider({'type': 'string'})
            except ConstructorCalled:
                pass
            else:
                raise ValueError('separate document operation did not select original constructor')
            cases['valid-and-invalid-with-original-constructor-forbidden'] = True
            cases['document-compatibility-selected-before-operation'] = True
        finally:
            original.__init__ = saved_init
    finally:
        for selected, meta in saved_meta.items():
            selected.META_SCHEMA = meta
    if len(cases) != 11 or not all(cases.values()):
        raise ValueError('all11 App backend boundary cases required')
    return cases
