import ast
import hashlib
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import build_components
from build_native import GIB, sandbox_command
import component_inputs
from fetch_sdk import inventory
import fetch_validation_tools as validation


class ComponentBuildTests(unittest.TestCase):
    def test_native_thread_capabilities_plan_pins_exact_diagnostic_world(self):
        repo = component_inputs.HERE.parents[1]
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory).resolve() / 'thread-capabilities'
            receipt = component_inputs.stage_native_thread_capabilities(repo, output)

            plan = json.loads((output / 'plan.json').read_bytes())
            self.assertEqual(receipt['files'], inventory(output))
            self.assertEqual(plan['world'], 'thread-capabilities')
            self.assertEqual(plan['entrypoint'], 'app')
            self.assertTrue(plan['stub_wasi'])
            self.assertEqual(plan['outer_exports'], [
                'tobkiri-ui:thread-capabilities/probe@1.0.0',
            ])
            self.assertEqual(plan['allowed_imports'], [
                'tobkiri-native:thread-capabilities/bindings@1.0.0',
            ])
            self.assertEqual({item['source'] for item in plan['sources']},
                             set(component_inputs.THREAD_CAPABILITIES_SOURCES))
            self.assertEqual(plan['sources'], receipt['source_files'])
            for item in plan['sources']:
                self.assertEqual(item['sha256'],
                                 component_inputs.THREAD_CAPABILITIES_SHA256[
                                     item['source']])
            probe_wit = (output / 'wit/probe.wit').read_text()
            bindings_wit = (output / 'wit/deps/native-bindings/bindings.wit').read_text()
            self.assertIn('package tobkiri-ui:thread-capabilities@1.0.0;', probe_wit)
            self.assertIn('run: func() -> string;', probe_wit)
            self.assertIn('ping: func() -> string;', probe_wit)
            self.assertIn('export probe;', probe_wit)
            self.assertIn(
                'package tobkiri-native:thread-capabilities@1.0.0;', bindings_wit,
            )
            self.assertIn('inspect: func(payload: string) -> string;', bindings_wit)
            self.assertEqual(receipt['plan_sha256'], hashlib.sha256(
                (output / 'plan.json').read_bytes()).hexdigest())
            with self.assertRaises(FileExistsError):
                component_inputs.stage_native_thread_capabilities(repo, output)

    def test_native_thread_capabilities_rejects_source_change_during_staging(self):
        source_repo = component_inputs.HERE.parents[1]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            repo = root / 'repo'
            app_source = repo / component_inputs.THREAD_CAPABILITIES_SOURCES[
                'python/app.py'
            ]
            for staged, source in component_inputs.THREAD_CAPABILITIES_SOURCES.items():
                path = repo / source
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes((source_repo / source).read_bytes())

            producer_dir = repo / 'tools/python313-producer'
            original_capture = component_inputs._capture_thread_capabilities_sources
            calls = 0

            def mutate_after_capture(source_repo):
                nonlocal calls
                captured = original_capture(source_repo)
                calls += 1
                if calls == 1:
                    app_source.write_bytes(b'modified app source')
                return captured

            with patch.object(component_inputs, 'HERE', producer_dir), patch.object(
                component_inputs,
                '_capture_thread_capabilities_sources',
                side_effect=mutate_after_capture,
            ):
                with self.assertRaisesRegex(ValueError, 'reviewed pin'):
                    component_inputs.stage_native_thread_capabilities(
                        repo, root / 'thread-capabilities',
                    )

    def test_native_thread_capabilities_selector_is_exclusive(self):
        with patch.object(build_components.sys, 'platform', 'linux'), patch.dict(
            os.environ, {'GITHUB_ACTIONS': 'true'},
        ):
            with self.assertRaisesRegex(ValueError, 'separate build scopes'):
                build_components.run(
                    None,
                    Path('/producer'),
                    Path('/validation'),
                    Path('/destination'),
                    all_five=True,
                    native_thread_capabilities=True,
                )

    def test_whole_native_harness_plan_loads_the_real_dispatcher_and_original_sources(self):
        repo = component_inputs.HERE.parents[1]
        original_plans = component_inputs.plans(True)
        previous_path = sys.path[:]
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory).resolve() / 'native-harness'
            receipt = component_inputs.stage_harness_native(repo, output)
            sys.path.insert(0, str(repo / 'sdk/python'))
            try:
                import build_component as sdk

                plan, _, plan_hash, captured = sdk._verify_plan(output / 'plan.json')
            finally:
                sys.path[:] = previous_path
            self.assertEqual(plan_hash, receipt['plan_sha256'])
            self.assertEqual(plan['world'], 'harness-native')
            self.assertEqual(plan['outer_exports'], [
                'tobkiri-ui:harness-native/runtime@1.0.0',
                'tobkiri-ui:harness-native/transport@1.0.0',
            ])
            self.assertEqual(plan['allowed_imports'], [
                'tobkiri-native:harness/bindings@1.0.0',
            ])
            actual = {str(path): data for path, data in captured}
            adapter = repo / 'tools/python313-producer/harness_native'
            self.assertEqual(actual['app.py'], (adapter / 'app.py').read_bytes())
            self.assertEqual(actual['harness_native/bootstrap.py'],
                             (adapter / 'bootstrap.py').read_bytes())
            runtime = ('ecosystem/defaultspack/defaultspack/'
                       'profile_runtime_composition.py')
            self.assertEqual(actual[runtime],
                             (repo / 'tobkiri_runtime' / runtime).read_bytes())
            self.assertEqual(len([name for name in actual
                                  if name.startswith('tobkiri_protocol/schemas/')]), 42)
            tree = ast.parse(actual['harness_native_source_index.py'])
            index = {n.targets[0].id: ast.literal_eval(n.value)
                     for n in tree.body if isinstance(n, ast.Assign)}
            self.assertEqual(len(index['MODULES']), 68)
            provenance = json.loads((output / 'provenance.json').read_bytes())
            self.assertFalse(provenance['external_dependencies_staged'])
            self.assertFalse(provenance['component_init_performed'])
            self.assertFalse(provenance['native_imports_connected'])
            self.assertFalse(provenance['production_enabled'])
            with self.assertRaises(FileExistsError):
                component_inputs.stage_harness_native(repo, output)
        self.assertEqual(component_inputs.plans(True), original_plans)
        self.assertEqual(sys.path, previous_path)

    def test_native_stack_world_is_independent_and_original_plans_remain_unchanged(self):
        repo = component_inputs.HERE.parents[1]
        original = component_inputs.plans(True)
        previous_path = sys.path[:]
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory).resolve() / 'stack'
            receipt = component_inputs.stage_native_input_stack(repo, output)
            plan = json.loads((output / 'plan.json').read_bytes())
            self.assertEqual(receipt['files'], inventory(output))
            self.assertEqual(plan['world'], 'native-input-stack')
            self.assertEqual(plan['outer_exports'], ['tobkiri-ui:native-input-stack/probe@1.0.0'])
            self.assertEqual(plan['allowed_imports'], ['tobkiri-ui:inputs/captured@1.0.0'])
            self.assertFalse(any('tobkiri:core' in value for value in
                                 plan['outer_exports'] + plan['allowed_imports']))
            self.assertEqual(receipt['plan_sha256'], hashlib.sha256(
                (output / 'plan.json').read_bytes()).hexdigest())
            with self.assertRaises(FileExistsError):
                component_inputs.stage_native_input_stack(repo, output)
        self.assertEqual(component_inputs.plans(True), original)
        self.assertEqual(sys.path, previous_path)

    def test_setup_candidate_uses_separate_source_plan_without_changing_originals(self):
        repo = component_inputs.HERE.parents[1]
        original_plans = component_inputs.plans(True)
        previous_path = sys.path[:]
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory).resolve() / 'setup'
            receipt = component_inputs.stage_harness_setup(repo, output)
            plan = json.loads((output / 'plan.json').read_bytes())
            self.assertEqual(len(plan['sources']), 11)
            self.assertEqual(receipt['files'], inventory(output))
            self.assertEqual(receipt['plan_sha256'],
                             hashlib.sha256((output / 'plan.json').read_bytes()).hexdigest())
            self.assertEqual(plan['outer_exports'], [
                'tobkiri:core/lifecycle@1.0.0', 'local:harness-setup-python/setup@1.0.0'])
            with self.assertRaises(FileExistsError):
                component_inputs.stage_harness_setup(repo, output)
        self.assertEqual(component_inputs.plans(True), original_plans)
        self.assertEqual(sys.path, previous_path)

    def test_all_current_sources_match_the_three_original_plans(self):
        repo = component_inputs.HERE.parents[1]
        counts = {}
        for name in component_inputs.PLANS:
            raw, captures, receipt = component_inputs.capture(repo, name)
            counts[name] = len(captures)
            self.assertEqual(hashlib.sha256(raw).hexdigest(), receipt['plan_sha256'])
            self.assertEqual(len(receipt['source_files']), len(captures))
        self.assertEqual(counts, {'usage-cost': 9, 'stream-normalizer': 9, 'finalizer': 13})
        self.assertEqual(component_inputs.REFERENCES[
            'wit/deps/ai-tool-bridge-ordered-python/pack.wit'],
            'packs/local.ai-tool-bridge-ordered-python/wit/pack-v2.wit')

    def test_complete_graph_reuses_exact_bridge_and_caller_source_stagers(self):
        repo = component_inputs.HERE.parents[1]
        original_path = sys.path[:]
        for name, count in (('bridge-v2', 12), ('caller', 10)):
            raw, captures, receipt = component_inputs.capture(repo, name)
            self.assertEqual(len(captures), count)
            self.assertEqual(hashlib.sha256(raw).hexdigest(), receipt['plan_sha256'])
        self.assertEqual(sys.path, original_path)
        self.assertEqual(len(component_inputs.plans(True)), 5)
        self.assertEqual(len(component_inputs.plans()), 3)

    def test_artifact_only_namespace_requires_no_cpython_source_mount(self):
        command = sandbox_command(None, Path('/work'), Path('/recipe'),
            ((Path('/verified/producer'), '/producer'),), address_space_bytes=8 * GIB)
        self.assertNotIn('/source/cpython', command)
        self.assertIn('--unshare-all', command)
        self.assertIn('--clearenv', command)
        self.assertEqual(command.count('--bind'), 1)
        self.assertIn(['--ro-bind', '/verified/producer', '/producer'],
                      [command[i:i+3] for i in range(len(command)-2)])

    def test_source_capture_rejects_tampering_and_staging_never_overwrites(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            here, repo = root / 'helper', root / 'repo'
            source = repo / 'packs/test-pack/python/app.py'
            source.parent.mkdir(parents=True)
            source.write_bytes(b'original bytes')
            plan = here / 'trials/2026-10-20-finalization/builds/test/plan.json'
            plan.parent.mkdir(parents=True)
            raw = json.dumps({'sources': [{'source': 'python/app.py', 'destination': 'app.py',
                             'sha256': hashlib.sha256(b'original bytes').hexdigest()}]}).encode()
            plan.write_bytes(raw)
            with patch.object(component_inputs, 'HERE', here), patch.object(
                    component_inputs, 'PLANS', {'test': ('test-pack', hashlib.sha256(raw).hexdigest())}):
                destination = root / 'stage'
                result = component_inputs.stage(repo, 'test', destination)
                self.assertEqual(result['files'], inventory(destination))
                self.assertEqual((destination / 'python/app.py').read_bytes(), b'original bytes')
                with self.assertRaises(FileExistsError):
                    component_inputs.stage(repo, 'test', destination)
                source.write_bytes(b'changed source')
                with self.assertRaisesRegex(ValueError, 'input changed'):
                    component_inputs.capture(repo, 'test')
                source.unlink()
                source.symlink_to(plan)
                with self.assertRaisesRegex(ValueError, 'linked'):
                    component_inputs.capture(repo, 'test')
                plan.write_bytes(b'changed plan')
                with self.assertRaisesRegex(ValueError, 'plan changed'):
                    component_inputs.capture(repo, 'test')

    def test_wheel_extraction_preserves_regular_bytes_without_execution(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            archive = root / 'input.whl'
            with zipfile.ZipFile(archive, 'w') as z:
                z.writestr('wasmtime/__init__.py', b'not executed')
                z.writestr('wasmtime/_lib/libwasmtime.so', b'native bytes')
            result = validation.extract_wheel(archive, root / 'extracted')
            self.assertEqual(set(result), {'wasmtime/__init__.py', 'wasmtime/_lib/libwasmtime.so'})
            self.assertEqual((root / 'extracted/wasmtime/__init__.py').read_bytes(), b'not executed')

    def test_wheel_paths_links_duplicates_and_budgets_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            cases = [('escape', ['../outside']), ('absolute', ['/outside']),
                     ('alias', ['package/a', 'package/A']), ('backslash', ['a\\b'])]
            for label, names in cases:
                archive = root / (label + '.whl')
                with zipfile.ZipFile(archive, 'w') as z:
                    for name in names:
                        z.writestr(name, b'content')
                with self.assertRaisesRegex(ValueError, 'unsafe'):
                    validation.extract_wheel(archive, root / label)
            self.assertFalse((root / 'outside').exists())
            archive = root / 'linked.whl'
            link = zipfile.ZipInfo('link')
            link.external_attr = (stat.S_IFLNK | 0o777) << 16
            with zipfile.ZipFile(archive, 'w') as z:
                z.writestr(link, b'outside')
            with self.assertRaisesRegex(ValueError, 'unsafe'):
                validation.extract_wheel(archive, root / 'linked')
            archive = root / 'bounded.whl'
            with zipfile.ZipFile(archive, 'w') as z:
                z.writestr('a', b'1234')
                z.writestr('b', b'5678')
            for index, (constant, value) in enumerate((('MAX_ENTRIES', 1), ('MAX_FILE', 3),
                                                       ('MAX_EXPANDED', 7))):
                with patch.object(validation, constant, value):
                    with self.assertRaises(ValueError):
                        validation.extract_wheel(archive, root / f'limited{index}')

    def test_validation_pins_and_component_budget_match_existing_evidence(self):
        repo = component_inputs.HERE.parents[1]
        lock = json.loads((repo / 'sdk/python/toolchain-lock.json').read_bytes())
        self.assertEqual(validation.INPUTS[1]['sha256'],
                         lock['tools']['wasmtime']['wheels']['py3-none-manylinux1_x86_64']['sha256'])
        tree = ast.parse((repo / 'sdk/python/composition.py').read_text())
        assignment = next(n for n in tree.body if isinstance(n, ast.Assign)
                          and any(isinstance(t, ast.Name) and t.id == 'COMPOSER_RELEASE_HASHES'
                                  for t in n.targets))
        self.assertEqual(validation.INPUTS[0]['sha256'],
                         ast.literal_eval(assignment.value)['x86_64-unknown-linux-musl'])
        evidence = json.loads((component_inputs.HERE /
            'trials/2026-10-20-finalization/builds/usage-cost/supervision.json').read_text())
        self.assertEqual(evidence['address_space_limit_per_process'], 8 * GIB)
        command = sandbox_command(Path('/source'), Path('/work'), Path('/recipe'),
                                  address_space_bytes=8 * GIB)
        self.assertIn('--unshare-all', command)
        self.assertIn('--clearenv', command)
        self.assertIn(f'--as={8 * GIB}', command)
        self.assertIn(f'--as={2 * GIB}', sandbox_command(Path('/source'), Path('/work'), Path('/recipe')))

    def test_desktop_cannot_download_tools_or_execute_components(self):
        with patch.object(validation.sys, 'platform', 'darwin'):
            with self.assertRaisesRegex(ValueError, 'Linux CI'):
                validation.run(Path('/unused'))
            with self.assertRaisesRegex(ValueError, 'Linux CI'):
                build_components.run(*[Path('/unused')] * 4)


if __name__ == '__main__':
    unittest.main()
