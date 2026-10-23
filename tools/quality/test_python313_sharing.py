"""Synthetic transport/provenance checks; never builds or executes a component."""
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'tools/python313-producer'))
from build_native import STAGED_SHA256, fingerprint
import component_inputs
import verify_original_components as verifier


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + '\n')


class OriginalGraphArtifactTests(unittest.TestCase):
    def fixture(self, root):
        # These bytes exercise transport only; the actual worker would reject
        # the synthetic body. This test never labels them as executed Wasm.
        receipt = {'format': 'tobkiri.python313-original-components/1',
                   'outcome': 'success', 'inputs_unchanged': True,
                   'source': {'sha256': STAGED_SHA256},
                   'producer': {'sha256': 'a' * 64},
                   'producer_receipt': {'sha256': 'b' * 64},
                   'native_core_replay': False, 'production_enabled': False, 'components': {}}
        for name in component_inputs.PLANS:
            raw, captures, source = component_inputs.capture(ROOT, name)
            plan = json.loads(raw)
            manifest = [{'source': x['source'], 'destination': x['destination'],
                         'sha256': hashlib.sha256(captures[x['source']]).hexdigest(),
                         'bytes': len(captures[x['source']])} for x in plan['sources']]
            directory = root / name / 'work'
            directory.mkdir(parents=True)
            artifact = directory / 'component.wasm'
            artifact.write_bytes(b'\0asm\x0d\0\x01\0synthetic-transport-only')
            identity = fingerprint(artifact)
            proof = {'format': 'tobkiri.component-build/2',
                     'build_mode': 'hash-bound-custom-python313-producer-ci',
                     'compiler_executable_sha256': 'a' * 64,
                     'producer_ci_receipt_sha256': 'b' * 64,
                     'build_plan_sha256': source['plan_sha256'], 'source_manifest': manifest,
                     'source_manifest_sha256': hashlib.sha256(json.dumps(manifest,
                         sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest(),
                     'artifact_sha256': identity['sha256'], 'artifact_bytes': identity['bytes'],
                     'imports': plan['allowed_imports'], 'exports': plan['outer_exports'],
                     'production_enabled': False, 'guest_runtime_identity_verified': False}
            path = directory / 'component.build.json'
            write(path, proof)
            receipt['components'][name] = {'artifact': identity, 'build_receipt': fingerprint(path)}
        write(root / 'components-build.json', receipt)
        return receipt

    def test_complete_transport_is_bound_to_current_original_source(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            self.fixture(root)
            result = verifier.verify(root)
            self.assertEqual(set(result['components']), set(component_inputs.PLANS))
            self.assertFalse(result['native_core_acceptance'])
            self.assertFalse(result['production_enabled'])

    def test_changed_bytes_and_unlisted_files_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            self.fixture(root)
            (root / 'extra').write_bytes(b'x')
            with self.assertRaisesRegex(ValueError, 'inventory'):
                verifier.verify(root)
            (root / 'extra').unlink()
            (root / 'usage-cost/work/component.wasm').write_bytes(b'changed')
            with self.assertRaisesRegex(ValueError, 'identity changed'):
                verifier.verify(root)

    def test_rehashed_receipt_cannot_change_the_original_source_or_producer(self):
        for key, changed in (('source_manifest', []), ('compiler_executable_sha256', 'c' * 64)):
            with self.subTest(key=key), tempfile.TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                receipt = self.fixture(root)
                path = root / 'finalizer/work/component.build.json'
                proof = json.loads(path.read_bytes())
                proof[key] = changed
                write(path, proof)
                receipt['components']['finalizer']['build_receipt'] = fingerprint(path)
                write(root / 'components-build.json', receipt)
                with self.assertRaisesRegex(ValueError, 'provenance changed'):
                    verifier.verify(root)

    def test_failed_receipt_and_artifact_links_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            receipt = self.fixture(root)
            receipt['outcome'] = 'failed'
            write(root / 'components-build.json', receipt)
            with self.assertRaisesRegex(ValueError, 'successful'):
                verifier.verify(root)
            receipt['outcome'] = 'success'
            write(root / 'components-build.json', receipt)
            artifact = root / 'usage-cost/work/component.wasm'
            saved = root / 'saved'
            artifact.rename(saved)
            artifact.symlink_to(saved)
            with self.assertRaisesRegex(ValueError, 'identity changed'):
                verifier.verify(root)


if __name__ == '__main__':
    unittest.main()
