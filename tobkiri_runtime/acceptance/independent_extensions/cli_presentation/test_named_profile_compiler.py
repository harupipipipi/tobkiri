"""Exercise the public formal Profile producer with our independent Pack."""
from __future__ import annotations

import copy
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).parent
RUNTIME = ROOT.parents[2]
# Standalone author tests need the repository root before the public API import.
sys.path.insert(0, str(RUNTIME))
from core_runtime.profile_authoring import build_named_profile  # noqa: E402

spec = importlib.util.spec_from_file_location('own_cli_builder', ROOT / 'build.py')
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)
PACK = ROOT / 'acceptance.cli.presentation'
TEMPLATE = RUNTIME / 'ecosystem/defaultspack/v4'


class NamedProfileCompilerTests(unittest.TestCase):
    """Compilation proves source pins; it does not authorize Shell execution."""

    def intent(self) -> dict:
        """Read only our own named Profile intent."""
        return json.loads((ROOT / 'independent.tauri.renderer.profile.intent.v1.json').read_text())

    def compile(self, destination: Path, intent: dict, roots: tuple = (PACK,)) -> dict:
        """Invoke the documented public producer and read its generated lock."""
        build_named_profile(destination, template_bundle=TEMPLATE,
                            intent=intent, additional_pack_roots=roots)
        return json.loads((destination / 'authored.profile.lock.v5.json').read_text())

    def test_single_selected_pack_is_exactly_pinned_and_unbound(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT) as temporary:
            lock = self.compile(Path(temporary) / 'release', self.intent())
        pack = json.loads((PACK / 'pack.v4.json').read_text())['pack']
        entries = [e for e in lock['effective_set'] if e['identity'] == pack['id']]
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]['artifact_digest'], pack['artifact_digest'])
        self.assertEqual(lock['activation_authority'], 'unbound')
        self.assertEqual(lock['profile_id'], 'independent.tauri.renderer')

    def test_supplied_unselected_pack_is_not_in_effective_closure(self) -> None:
        intent = self.intent()
        intent['profile_id'] = 'independent.tauri.unselected'
        intent['packs'] = [p for p in intent['packs'] if p['role'] == 'application']
        with tempfile.TemporaryDirectory(dir=ROOT) as temporary:
            lock = self.compile(Path(temporary) / 'release', intent)
        self.assertNotIn('acceptance.cli.presentation',
                         [e['identity'] for e in lock['effective_set']])

    def test_rebuilt_renamed_pack_is_selected_by_explicit_identity(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT) as temporary:
            renamed = Path(temporary) / 'renamed-pack'
            try:
                builder.build(pack_id='independent.renamed.renderer',
                              contract_id='independent.renamed.transcript.v1',
                              function_id='independent.renamed.render',
                              operation_id='independent.renamed.present',
                              destination=renamed)
                intent = self.intent()
                intent['profile_id'] = 'independent.tauri.renamed'
                intent['packs'][1]['pack_id'] = 'independent.renamed.renderer'
                lock = self.compile(Path(temporary) / 'release', intent, (renamed,))
            finally:
                builder.build(destination=Path(temporary) / 'restore-authoring')
        identities = [e['identity'] for e in lock['effective_set']]
        self.assertIn('independent.renamed.renderer', identities)
        self.assertNotIn('acceptance.cli.presentation', identities)

    def test_unknown_selected_identity_is_denied_before_publication(self) -> None:
        intent = copy.deepcopy(self.intent())
        intent['packs'][1]['pack_id'] = 'independent.unknown.renderer'
        with tempfile.TemporaryDirectory(dir=ROOT) as temporary:
            destination = Path(temporary) / 'release'
            with self.assertRaisesRegex(ValueError, 'Profile Pack is unavailable'):
                self.compile(destination, intent)
            self.assertFalse(destination.exists())

    def test_duplicate_additional_pack_is_denied_before_publication(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT) as temporary:
            destination = Path(temporary) / 'release'
            with self.assertRaisesRegex(Exception, 'additional Pack identity is duplicate'):
                self.compile(destination, self.intent(), (PACK, PACK))
            self.assertFalse(destination.exists())


if __name__ == '__main__':
    unittest.main()
