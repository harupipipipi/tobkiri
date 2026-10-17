"""Public-schema author checks; no Profile compilation or activation claims."""
from __future__ import annotations

import copy
import importlib.util
import json
import unittest
from pathlib import Path

from jsonschema import ValidationError

ROOT = Path(__file__).parent
RUNTIME = ROOT.parents[2]
spec = importlib.util.spec_from_file_location('profile_author', ROOT / 'profile_authoring.py')
author = importlib.util.module_from_spec(spec)
spec.loader.exec_module(author)


class ProfileAuthoringTests(unittest.TestCase):
    """Exercise identity selection, explicit Application and denial boundaries."""

    def inputs(self) -> dict:
        """Read published declaration bytes and our own generated Pack only."""
        bundle = RUNTIME / 'ecosystem/defaultspack/v4'
        return {
            'profile_id': 'independent.tauri.renderer', 'display_name': 'Tobkiri renderer',
            'base_definition': json.loads((bundle / 'defaults-basepack.base.v1.json').read_text()),
            'shell_definition': json.loads((bundle / 'shell.tauri.default.shell.v1.json').read_text()),
            'application_manifest': json.loads((bundle / 'packs/runtime.tauri.application.default.pack.v4.json').read_text()),
            'selected_pack_manifests': [json.loads((ROOT / 'acceptance.cli.presentation/pack.v4.json').read_text())],
        }

    def test_named_intent_selects_published_ids_and_renderer(self) -> None:
        intent = author.make_profile_intent(**self.inputs())
        self.assertEqual(intent['base']['pack_id'], 'defaults-basepack')
        self.assertEqual(intent['shell']['provider_id'], 'shell.tauri.default')
        self.assertEqual([p['pack_id'] for p in intent['packs']],
                         ['runtime.tauri.application.default', 'acceptance.cli.presentation'])
        self.assertEqual(intent['requested_edges'], [])
        self.assertEqual(intent['authority_references'], [])
        self.assertEqual(intent['state'], 'needs_resolution')

    def test_parameterized_profile_and_selected_pack_identity(self) -> None:
        inputs = copy.deepcopy(self.inputs())
        inputs['profile_id'] = 'another.named.profile'
        inputs['selected_pack_manifests'][0]['pack']['id'] = 'independent.renamed.renderer'
        intent = author.make_profile_intent(**inputs)
        self.assertEqual(intent['profile_id'], 'another.named.profile')
        self.assertEqual(intent['packs'][1]['pack_id'], 'independent.renamed.renderer')
        author.validate_intent(intent)

    def test_schema_denies_malformed_profile_identity(self) -> None:
        inputs = self.inputs()
        inputs['profile_id'] = '../profile'
        with self.assertRaises(ValidationError):
            author.make_profile_intent(**inputs)

    def test_denies_non_application_and_duplicate_selection(self) -> None:
        inputs = self.inputs()
        inputs['application_manifest']['pack']['kind'] = 'normal_sandbox'
        with self.assertRaisesRegex(ValueError, 'Application'):
            author.make_profile_intent(**inputs)
        inputs = self.inputs()
        inputs['selected_pack_manifests'] *= 2
        with self.assertRaisesRegex(ValueError, 'duplicate'):
            author.make_profile_intent(**inputs)

    def test_denies_unpublished_shell_target(self) -> None:
        inputs = self.inputs()
        inputs.update(platform='windows', architecture='arm64')
        with self.assertRaisesRegex(ValueError, 'build target'):
            author.make_profile_intent(**inputs)

    def test_schema_denies_source_approval_or_bound_digest(self) -> None:
        intent = author.make_profile_intent(**self.inputs())
        with self.assertRaises(ValidationError):
            author.validate_intent({**intent, 'approved': True})
        intent['catalog_revision'] = 'sha256:' + '1' * 64
        with self.assertRaises(ValidationError):
            author.validate_intent(intent)


if __name__ == '__main__':
    unittest.main()
