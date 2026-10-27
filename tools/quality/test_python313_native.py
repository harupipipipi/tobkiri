"""Synthetic native input transport tests; no real component or Core executes."""
import json
from pathlib import Path
import sys
import tempfile
import unittest

import test_python313_sharing as sharing
ROOT, write = sharing.ROOT, sharing.write
sys.path.insert(0, str(ROOT / 'tools/python313-producer'))
from build_native import fingerprint
import native_graph


class NativeGraphTransportTests(unittest.TestCase):
    def fixture(self, root):
        artifacts = root / 'original-five-artifacts'
        artifacts.mkdir()
        sharing.OriginalGraphArtifactTests().fixture(artifacts, all_five=True)
        shared = root / 'original-five-shared'
        shared.mkdir()
        records = []
        module = b'\0asm\x01\0\0\0'
        component = b'\0asm\x0d\0\x01\0'
        for index, (_, name) in enumerate(native_graph.ROLES):
            directory = shared / str(index)
            directory.mkdir()
            (directory / 'component.wasm').write_bytes(component)
            (directory / 'module-0.wasm').write_bytes(module)
            original = fingerprint(artifacts / name / 'work/component.wasm')
            records.append({'source_bytes': original['bytes'], 'source_sha256': original['sha256'],
                            'component': {**fingerprint(directory / 'component.wasm'),
                                          'modules': [{**fingerprint(directory / 'module-0.wasm'),
                                                       'import': 'unbundled-module0'}]}})
        receipt = {'format': 'tobkiri.shared-core-module-materialization/1',
                   'guest_execution_performed': False, 'native_core_acceptance': False,
                   'production_enabled': False, 'components': records,
                   'unique_modules': 1, 'catalog_source_bytes': 5 * len(component) + len(module)}
        write(shared / 'extraction.json', receipt)
        provider, bridge = root / 'provider.json', root / 'bridge.json'
        # Semantic source-oracle verification is performed by the Rust fixture.
        provider.write_bytes(b'{}')
        bridge.write_bytes(b'{}')
        return provider, bridge, receipt

    def test_every_role_is_original_python_and_every_oracle_is_explicit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            provider, bridge, _ = self.fixture(root)
            values = native_graph.values(root, provider, bridge, root / 'new-evidence')
            for role, name in native_graph.ROLES:
                self.assertEqual(values[native_graph.PREFIX + role + '_IMPLEMENTATION'], 'python')
                self.assertEqual(values[native_graph.PREFIX + role + '_COMPONENT'],
                                 str(root / 'original-five-artifacts' / name / 'work/component.wasm'))
            for oracle in ('ORACLE', 'BRIDGE_ORACLE', 'CALLER_ORACLE', 'FINALIZER_ORACLE'):
                self.assertIn(native_graph.PREFIX + oracle, values)
                self.assertEqual(len(values[native_graph.PREFIX + oracle + '_SHA256']), 64)
            self.assertFalse((root / 'new-evidence').exists())

    def test_wrong_original_order_and_changed_extracted_bytes_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            provider, bridge, receipt = self.fixture(root)
            receipt['components'][0]['source_sha256'] = 'f' * 64
            write(root / 'original-five-shared/extraction.json', receipt)
            with self.assertRaisesRegex(ValueError, 'different original'):
                native_graph.values(root, provider, bridge, root / 'new-evidence')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            provider, bridge, _ = self.fixture(root)
            (root / 'original-five-shared/0/module-0.wasm').write_bytes(b'changed!')
            with self.assertRaisesRegex(ValueError, 'identity changed'):
                native_graph.values(root, provider, bridge, root / 'new-evidence')

    def test_extra_code_entries_invalid_counts_and_claimed_execution_reject(self):
        for field, value in (('unique_modules', 2), ('catalog_source_bytes', 64 * 1024 * 1024 + 1),
                             ('native_core_acceptance', True)):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                provider, bridge, receipt = self.fixture(root)
                receipt[field] = value
                write(root / 'original-five-shared/extraction.json', receipt)
                with self.assertRaises(ValueError):
                    native_graph.values(root, provider, bridge, root / 'new-evidence')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            provider, bridge, _ = self.fixture(root)
            (root / 'original-five-shared/extra').write_bytes(b'x')
            with self.assertRaisesRegex(ValueError, 'inventory'):
                native_graph.values(root, provider, bridge, root / 'new-evidence')

    def test_existing_evidence_and_linked_inputs_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            provider, bridge, _ = self.fixture(root)
            evidence = root / 'existing'
            evidence.mkdir()
            with self.assertRaisesRegex(ValueError, 'fresh'):
                native_graph.values(root, provider, bridge, evidence)
            provider.unlink()
            provider.symlink_to(bridge)
            with self.assertRaisesRegex(ValueError, 'linked source oracle'):
                native_graph.values(root, provider, bridge, root / 'new-evidence')


if __name__ == '__main__':
    unittest.main()
