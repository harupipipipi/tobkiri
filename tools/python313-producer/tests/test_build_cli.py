import hashlib
import json
from pathlib import Path
import sys
import tempfile
import tomllib
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import build_cli
import cli_source
from build_native import fingerprint
from fetch_sdk import inventory
from packager.package import ASSETS

ORIGINAL = b'''[package]
name = "componentize-py"
version.workspace = true
[dependencies]
anyhow = "1.0.91"
[build-dependencies]
generator = { path = "test-generator" }
[workspace]
package.version = "0.25.0"
'''


class CliBuildTests(unittest.TestCase):
    def fixture(self, root):
        source, assets = root / 'original', root / 'assets'
        source.mkdir()
        assets.mkdir()
        (source / 'Cargo.toml').write_bytes(ORIGINAL)
        (source / 'build.rs').write_bytes(b'preserve original normal script')
        (source / 'alt-build.rs').write_bytes(b'preserve original publishing script')
        (source / 'nested').mkdir()
        (source / 'nested/executable').write_bytes(b'preserve bytes and mode')
        (source / 'nested/executable').chmod(0o755)
        expected = {}
        for name in ASSETS:
            path = assets / name
            path.write_bytes(b'verified packaged fixture ' + name.encode())
            expected[name] = fingerprint(path)
        return source, assets, expected

    def test_only_root_build_selector_changes(self):
        updated = cli_source.publishing_manifest(ORIGINAL)
        self.assertEqual(updated.replace(b'build = "alt-build.rs"\n', b'', 1), ORIGINAL)
        before, after = tomllib.loads(ORIGINAL.decode()), tomllib.loads(updated.decode())
        self.assertEqual(after.pop('package'), {**before.pop('package'), 'build': 'alt-build.rs'})
        self.assertEqual(before, after)
        for invalid in (updated, ORIGINAL.replace(b'componentize-py', b'other-package')):
            with self.assertRaises(ValueError):
                cli_source.publishing_manifest(invalid)

    def test_exact_original_source_and_all_assets_are_retained(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            source, assets, expected_assets = self.fixture(root)
            before = inventory(source)
            destination = root / 'derived'
            result = cli_source.stage(source, assets, expected_assets, destination)
            self.assertEqual(inventory(source), before)
            self.assertEqual(result['original_files'], before)
            self.assertEqual(result['files'], inventory(destination))
            self.assertEqual(set(result['files']), set(before) | ASSETS)
            for name, entry in before.items():
                if name != 'Cargo.toml':
                    self.assertEqual(result['files'][name], entry)
            for name, identity in expected_assets.items():
                self.assertEqual(fingerprint(destination / name), identity)
            with self.assertRaises(FileExistsError):
                cli_source.stage(source, assets, expected_assets, destination)

    def test_incomplete_changed_or_preexisting_asset_inputs_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            source, assets, expected = self.fixture(root)
            incomplete = dict(expected)
            incomplete.pop('bundled.tar.zst')
            with self.assertRaisesRegex(ValueError, 'complete assets'):
                cli_source.stage(source, assets, incomplete, root / 'incomplete')
            (assets / 'libc.so.zst').write_bytes(b'changed')
            with self.assertRaisesRegex(ValueError, 'input changed'):
                cli_source.stage(source, assets, expected, root / 'changed')
            (source / 'libc.so.zst').write_bytes(b'unexpected')
            with self.assertRaisesRegex(ValueError, 'unexpectedly contains'):
                cli_source.stage(source, assets, expected, root / 'preexisting')

    def test_source_links_and_unrecorded_recipe_are_not_accepted(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            source, assets, expected = self.fixture(root)
            (source / 'linked').symlink_to('alt-build.rs')
            with self.assertRaisesRegex(ValueError, 'source links'):
                cli_source.stage(source, assets, expected, root / 'derived')
        script = build_cli.HERE / 'trials/2026-10-19/producer-cli.sh'
        record = json.loads((script.parent / 'producer-receipt.json').read_text())
        self.assertEqual(hashlib.sha256(script.read_bytes()).hexdigest(), build_cli.RECIPE_SHA256)
        self.assertEqual(record['script_sha256']['producer-cli.sh'], build_cli.RECIPE_SHA256)
        self.assertIn('cargo build --frozen -j1 --release', script.read_text())

    def test_desktop_invocation_cannot_start_build_or_startup(self):
        with patch.object(build_cli.sys, 'platform', 'darwin'):
            with self.assertRaisesRegex(ValueError, 'Linux CI'):
                build_cli.run(*[Path('/unused')] * 8)


if __name__ == '__main__':
    unittest.main()
