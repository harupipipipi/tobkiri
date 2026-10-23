import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import build_runtimes as runtimes
import runtime_inputs
import verify_git_checkout as checkout
from build_native import GIB, sandbox_command
from fetch_sdk import inventory


def blob(data):
    return hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest()


class RuntimeBuildTests(unittest.TestCase):
    def test_original_recipe_and_recorded_limits_preserve_native_defaults(self):
        recorded = json.loads((runtimes.HERE / "trials/2026-10-19/producer-receipt.json").read_text())
        self.assertEqual(runtimes.RECIPE_SHA256, recorded["script_sha256"]["wasi-runtimes.sh"])
        recipe = runtimes.HERE / "trials/2026-10-19/wasi-runtimes.sh"
        self.assertEqual(hashlib.sha256(recipe.read_bytes()).hexdigest(), runtimes.RECIPE_SHA256)
        self.assertEqual(recorded["runtimes"]["limits"]["address_space_per_process"], 4 * GIB)
        self.assertIn(f"--as={2 * GIB}", sandbox_command(Path('/source'), Path('/work'), recipe))
        with self.assertRaises(ValueError):
            sandbox_command(Path('/source'), Path('/work'), recipe, address_space_bytes=16 * GIB)

    def test_no_network_or_host_home_and_frozen_source_cache_mounts(self):
        paths = [Path('/input') / name for name in ('source', 'registry', 'rust', 'git', 'wasi')]
        work = Path('/private/work')
        for frozen in (False, True):
            mounts = runtimes.mounts(*paths, work, frozen=frozen)
            command = sandbox_command(paths[0], work, Path('/recipe'), mounts,
                                      address_space_bytes=4 * GIB)
            self.assertIn('--unshare-all', command)
            self.assertIn('--clearenv', command)
            self.assertNotIn('--share-net', command)
            self.assertEqual(command.count('--bind'), 1)
            self.assertEqual(command[command.index('--bind') + 1:command.index('--bind') + 3],
                             [str(work), '/work'])
            for original, target in mounts:
                self.assertIn(['--ro-bind', str(original), target],
                              [command[i:i+3] for i in range(len(command)-2)])
            cache_target = '/work/runtime-cargo-home/git'
            self.assertEqual(any(target == cache_target for _, target in mounts), frozen)
        self.assertIn('GIT_ALLOW_PROTOCOL=file', runtimes.CACHE_RECIPE)
        self.assertIn('/rust/bin/cargo fetch --locked', runtimes.CACHE_RECIPE)
        self.assertIn('/rust/bin/cargo fetch --frozen', runtimes.CACHE_RECIPE)

    def test_actual_inventory_rejects_changed_missing_and_extra_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            path = root / 'input'
            path.write_bytes(b'original')
            expected = inventory(root)
            self.assertEqual(runtime_inputs.recorded_files(root, expected), expected)
            path.write_bytes(b'changed!')
            with self.assertRaises(ValueError):
                runtime_inputs.recorded_files(root, expected)
            path.unlink()
            with self.assertRaises(ValueError):
                runtime_inputs.recorded_files(root, expected)
            path.write_bytes(b'original')
            (root / 'extra').write_bytes(b'x')
            with self.assertRaises(ValueError):
                runtime_inputs.recorded_files(root, expected)

    def test_git_blob_identity_mode_and_links_are_checked(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            path = root / 'source.rs'
            data = b'original'
            path.write_bytes(data)
            path.chmod(0o644)
            checkout.verify_file(path, len(data), '100644', blob(data))
            path.write_bytes(b'changed!')
            with self.assertRaisesRegex(ValueError, 'pinned blob'):
                checkout.verify_file(path, len(data), '100644', blob(data))
            path.write_bytes(data)
            path.chmod(0o755)
            with self.assertRaisesRegex(ValueError, 'mode'):
                checkout.verify_file(path, len(data), '100644', blob(data))
            path.unlink()
            (root / 'outside').write_bytes(data)
            path.symlink_to('outside')
            with self.assertRaisesRegex(ValueError, 'type'):
                checkout.verify_file(path, len(data), '100644', blob(data))

    def test_checkout_inventory_checks_extra_files_and_nested_submodule(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            source = root / 'checkout'
            source.mkdir()
            (source / 'lib.rs').write_bytes(b'original')
            child = source / 'tests/testsuite'
            child.mkdir(parents=True)
            (child / 'fixture.wast').write_bytes(b'fixture')
            first = (f'100644 blob {blob(b"original")}       8\tlib.rs\0'
                     f'160000 commit {checkout.INPUTS[1]["commit"]}       -\ttests/testsuite\0').encode()
            second = f'100644 blob {blob(b"fixture")}       7\tfixture.wast\0'.encode()
            def read(repository, *args):
                return second if repository.name == 'testsuite.git' else first
            with patch.object(checkout, 'read_git', side_effect=read):
                totals = {'files': 0, 'bytes': 0}
                checkout.verify_checkout(root / 'wasm-tools.git', source,
                                         checkout.INPUTS[0], root, totals)
                self.assertEqual(totals, {'files': 2, 'bytes': 15})
                (source / 'extra.rs').write_bytes(b'extra')
                with self.assertRaisesRegex(ValueError, 'unlisted'):
                    checkout.verify_checkout(root / 'wasm-tools.git', source,
                        checkout.INPUTS[0], root, {'files': 0, 'bytes': 0})
                (source / 'extra.rs').unlink()
                (child / 'fixture.wast').write_bytes(b'changed')
                with self.assertRaisesRegex(ValueError, 'pinned blob'):
                    checkout.verify_checkout(root / 'wasm-tools.git', source,
                        checkout.INPUTS[0], root, {'files': 0, 'bytes': 0})

    def test_failed_receipt_and_local_run_cannot_start_build(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            receipt = root / 'input.json'
            receipt.write_text('{"outcome":"failed"}')
            with self.assertRaisesRegex(ValueError, 'did not succeed'):
                runtime_inputs.report(receipt)
            with patch.object(runtimes.sys, 'platform', 'darwin'), \
                    patch.object(runtimes, 'run_stage', side_effect=AssertionError('build started')):
                with self.assertRaisesRegex(ValueError, 'Linux CI'):
                    runtimes.run(root, root, root, root, root, root / 'new')
                self.assertFalse((root / 'new').exists())


if __name__ == '__main__':
    unittest.main()
