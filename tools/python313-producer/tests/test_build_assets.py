import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import asset_inputs
import build_assets
from build_native import fingerprint
from packager import package


class AssetBuildTests(unittest.TestCase):
    def inputs(self, root):
        inputs = {}
        for asset in sorted(package.ASSETS):
            logical = asset.removesuffix('.zst')
            path = root / logical
            if asset in package.TAR_ASSETS:
                path.mkdir()
                (path / 'original.py').write_bytes(b'original source\n')
                (path / 'nested').mkdir()
                (path / 'nested/keep.txt').write_bytes(b'preserve other files')
                (path / 'excluded.pyc').write_bytes(b'excluded')
                (path / 'Makefile').write_bytes(b'excluded')
            else:
                path.write_bytes(b'\0asm' + asset.encode())
            inputs[asset] = (path, logical, None)
        return inputs

    def test_manifest_captures_all_original_bytes_and_tar_inclusion(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            inputs = self.inputs(root)
            manifest, mounts, identities = asset_inputs.manifest(inputs)
            groups = package.capture(root, manifest)
            self.assertEqual(set(groups), package.ASSETS)
            self.assertEqual(len(mounts), 13)
            self.assertEqual(len(identities), 15)
            for asset in package.TAR_ASSETS:
                self.assertEqual({r['member']: r['data'] for r in groups[asset]},
                                 {'original.py': b'original source\n',
                                  'nested/keep.txt': b'preserve other files'})
            self.assertEqual(asset_inputs.manifest(inputs)[0], manifest)
            before = json.loads(manifest)
            (root / 'bundled.tar/original.py').write_bytes(b'changed')
            self.assertNotEqual(json.loads(asset_inputs.manifest(inputs)[0]), before)

    def test_adapter_pin_missing_assets_and_empty_tar_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            inputs = self.inputs(Path(directory).resolve())
            adapter = 'wasi_snapshot_preview1.reactor.wasm.zst'
            path, logical, _ = inputs[adapter]
            inputs[adapter] = (path, logical, '0' * 64)
            with self.assertRaisesRegex(ValueError, 'adapter identity'):
                asset_inputs.manifest(inputs)
            inputs[adapter] = (path, logical, None)
            removed = inputs.pop(adapter)
            with self.assertRaisesRegex(ValueError, 'thirteen'):
                asset_inputs.manifest(inputs)
            inputs[adapter] = removed
            tar = inputs['bundled.tar.zst'][0]
            (tar / 'original.py').unlink()
            (tar / 'nested/keep.txt').unlink()
            with self.assertRaisesRegex(ValueError, 'empty'):
                asset_inputs.manifest(inputs)

    def test_links_special_entries_and_aggregate_limits_fail_before_execution(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            inputs = self.inputs(root)
            tar = inputs['bundled.tar.zst'][0]
            (tar / 'linked').symlink_to(root, target_is_directory=True)
            with self.assertRaisesRegex(ValueError, 'linked or special'):
                asset_inputs.manifest(inputs)
            (tar / 'linked').unlink()
            with patch.object(package, 'MAX_TOTAL', 1):
                with self.assertRaisesRegex(ValueError, 'aggregate bound'):
                    asset_inputs.manifest(inputs)
            with patch.object(package, 'MAX_RECORDS', 1):
                with self.assertRaisesRegex(ValueError, 'aggregate bound'):
                    asset_inputs.manifest(inputs)
            path, logical, pin = inputs['libc.so.zst']
            path.rename(root / 'target')
            path.symlink_to(root / 'target')
            with self.assertRaisesRegex(ValueError, 'physical'):
                asset_inputs.manifest(inputs)

    def test_current_build_output_receipt_is_required_and_tampering_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            identities = {}
            for name in package.ASSETS:
                (root / name).write_bytes(b'compressed-test-fixture-' + name.encode())
                identities[name] = fingerprint(root / name)
            receipt = {'status': 'packaged; not runtime validated',
                       'input_manifest_sha256': 'a' * 64, 'worker_sha256': 'b' * 64,
                       'artifacts': identities}
            path = root / 'package-receipt.json'
            path.write_text(json.dumps(receipt))
            self.assertEqual(build_assets.verify_outputs(root, 'a' * 64, 'b' * 64), identities)
            with self.assertRaisesRegex(ValueError, 'matching successful receipt'):
                build_assets.verify_outputs(root, 'c' * 64, 'b' * 64)
            (root / 'extra').write_bytes(b'x')
            with self.assertRaisesRegex(ValueError, 'inventory'):
                build_assets.verify_outputs(root, 'a' * 64, 'b' * 64)
            (root / 'extra').unlink()
            (root / 'libpython3.13.so.zst').write_bytes(b'changed')
            with self.assertRaisesRegex(ValueError, 'differs from its receipt'):
                build_assets.verify_outputs(root, 'a' * 64, 'b' * 64)

    def test_packaging_uses_new_hashes_and_recorded_adapter_without_top_level_build(self):
        recorded = json.loads((build_assets.HERE / 'trials/2026-10-19/producer-receipt.json').read_text())
        adapter = recorded['packaging']['validation']['assets']['wasi_snapshot_preview1.reactor.wasm.zst']
        self.assertEqual(asset_inputs.ADAPTER_SHA256, adapter['decoded_sha256'])
        recipe = build_assets.package_recipe('a' * 64, 'b' * 64)
        self.assertIn('--manifest-sha256 ' + 'a' * 64, recipe)
        self.assertIn('--worker-sha256 ' + 'b' * 64, recipe)
        with self.assertRaises(ValueError):
            build_assets.package_recipe('"; malicious', 'b' * 64)
        self.assertIn('cargo test --frozen -j1', build_assets.WORKER_RECIPE)
        self.assertIn('cargo build --frozen -j1', build_assets.WORKER_RECIPE)
        self.assertIn('--manifest-path /helper/Cargo.toml', build_assets.WORKER_RECIPE)
        self.assertNotIn('/source/componentize-py', build_assets.WORKER_RECIPE)
        self.assertNotIn('STUB', build_assets.WORKER_RECIPE)

    def test_desktop_invocation_cannot_start_a_build(self):
        with patch.object(build_assets.sys, 'platform', 'darwin'):
            with self.assertRaisesRegex(ValueError, 'Linux CI'):
                build_assets.run(*[Path('/unused')] * 7)


if __name__ == '__main__':
    unittest.main()
