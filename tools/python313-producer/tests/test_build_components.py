import ast
import hashlib
import json
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
