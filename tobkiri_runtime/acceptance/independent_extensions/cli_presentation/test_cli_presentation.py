"""Offline source/schema tests; never substitutes for Host/Broker/VM acceptance."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import unittest
import tempfile
import sys
from pathlib import Path

from jsonschema import Draft202012Validator
from referencing import Registry, Resource

ROOT = Path(__file__).parent
PACK = ROOT / 'acceptance.cli.presentation'
RUNTIME = ROOT.parents[2]
# Standalone discovery needs the repository import root before this public API.
sys.path.insert(0, str(RUNTIME))
from tobkiri_host.artifact_compiler import compile_pack_root  # noqa: E402

build_spec = importlib.util.spec_from_file_location('cli_build', ROOT / 'build.py')
builder = importlib.util.module_from_spec(build_spec)
build_spec.loader.exec_module(builder)
spec = importlib.util.spec_from_file_location('cli_render', ROOT / 'source/render.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class CliPresentationTests(unittest.TestCase):
    """Verify presentation semantics and closed, unbound authoring declarations."""

    def payload(self, text: str = 'offline hello') -> dict:
        """Return bounded source input without authority fields."""
        return {'messages': [{'role': 'assistant', 'text': text}],
                'columns': 40, 'output_limit': 4096}

    def test_offline_transcript_success(self) -> None:
        self.assertEqual(module.render(self.payload())['stdout'],
                         '[assistant]\noffline hello\n')

    def test_controls_cannot_inject_terminal_actions(self) -> None:
        output = module.render(self.payload('\x1b]52;c;secret\x07\u202e'))['stdout']
        self.assertNotIn('\x1b', output)
        self.assertNotIn('\x07', output)
        self.assertNotIn('\u202e', output)
        self.assertIn('\\u001b', output)

    def test_denies_approval_and_identity_injection(self) -> None:
        for key in ('approved', 'profile_id', 'provider_id', 'command', 'path'):
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, 'invalid_input'):
                module.render({**self.payload(), key: True})

    def test_denies_unknown_roles_and_message_keys(self) -> None:
        for message in ({'role': 'host', 'text': 'hello'},
                        {'role': 'user', 'text': 'hello', 'approved': True}):
            with self.subTest(message=message), self.assertRaises(ValueError):
                module.render({**self.payload(), 'messages': [message]})

    def test_denies_boolean_bounds(self) -> None:
        for key in ('columns', 'output_limit'):
            with self.subTest(key=key), self.assertRaises(ValueError):
                module.render({**self.payload(), key: True})

    def test_byte_limit_denies_multibyte_without_partial_output(self) -> None:
        with self.assertRaisesRegex(ValueError, 'output_limit'):
            module.render({**self.payload('あ' * 10), 'output_limit': 20})

    def test_wrapping_and_blank_lines_are_preserved(self) -> None:
        result = module.render({**self.payload('word ' * 10 + '\n\nend'), 'columns': 20})
        self.assertIn('\n\nend\n', result['stdout'])
        self.assertTrue(all(len(line) <= 20 for line in result['stdout'].splitlines()))

    def test_public_schemas_validate_all_declarations(self) -> None:
        registry = Registry()
        schema_dir = RUNTIME / 'tobkiri_protocol/schemas'
        for path in schema_dir.glob('*.schema.json'):
            document = json.loads(path.read_text())
            registry = registry.with_resource(document['$id'], Resource.from_contents(document))
        for filename, schema_name in (
                ('pack.v4.json', 'pack_manifest_v4'),
                ('contracts.v4.json', 'pack_contract_catalog_v4'),
                ('executables.v4.json', 'executable_catalog_v4'),
                ('artifact-index.v4.json', 'pack_artifact_index_v4')):
            with self.subTest(filename=filename):
                schema = json.loads((schema_dir / f'{schema_name}.schema.json').read_text())
                Draft202012Validator(schema, registry=registry).validate(
                    json.loads((PACK / filename).read_text()))
        schema = json.loads((schema_dir / 'pack_contract_catalog_v4.schema.json').read_text())
        Draft202012Validator(schema, registry=registry).validate(
            json.loads((ROOT / 'contracts.draft.v4.json').read_text()))
        schema = json.loads((schema_dir / 'profile_intent_v1.schema.json').read_text())
        Draft202012Validator(schema, registry=registry).validate(
            json.loads((ROOT / 'cli.profile.intent.v1.json').read_text()))

    def test_index_exact_file_bytes(self) -> None:
        index = json.loads((PACK / 'artifact-index.v4.json').read_text())
        for artifact in index['artifacts']:
            digest = 'sha256:' + hashlib.sha256((PACK / artifact['path']).read_bytes()).hexdigest()
            self.assertEqual(artifact['digest'], digest)

    def test_request_output_conforms_to_declared_schema(self) -> None:
        contract = json.loads((ROOT / 'contracts.draft.v4.json').read_text())['contracts'][0]
        operation = contract['operations'][0]
        schemas = contract['schema_catalog']
        Draft202012Validator(schemas[operation['input_schema_digest']]).validate(self.payload())
        Draft202012Validator(schemas[operation['output_schema_digest']]).validate(
            module.render(self.payload()))

    def test_public_compiler_has_exact_existing_python_route(self) -> None:
        compiled = compile_pack_root(PACK)
        route = compiled.routes[(
            'acceptance.presentation.transcript.v1',
            'acceptance.presentation.render')]
        self.assertEqual(route['runtime_abi'], 'python3.13')
        self.assertEqual(route['backend'], 'tobkiri.python-pack-v4')
        manifest = json.loads((PACK / 'pack.v4.json').read_text())
        self.assertEqual(manifest['pack']['kind'], 'normal_sandbox')
        self.assertEqual(manifest['requirements']['capabilities'], [])

    def test_existing_abi_denies_unknown_operation(self) -> None:
        with self.assertRaisesRegex(ValueError, 'unknown operation'):
            module.tobkiri_packvm_invoke('unknown.operation', self.payload())
        result = module.tobkiri_packvm_invoke(
            'acceptance.presentation.render', self.payload())
        self.assertEqual(result, module.render(self.payload()))

    def test_public_builder_output_is_byte_identical(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT) as temporary:
            first, second = Path(temporary) / 'first', Path(temporary) / 'second'
            builder.build(destination=first)
            builder.build(destination=second)
            first_files = {str(p.relative_to(first)): p.read_bytes()
                           for p in first.rglob('*') if p.is_file()}
            second_files = {str(p.relative_to(second)): p.read_bytes()
                            for p in second.rglob('*') if p.is_file()}
            self.assertEqual(first_files, second_files)

    def test_profile_has_no_activation_authority(self) -> None:
        intent = json.loads((ROOT / 'cli.profile.intent.v1.json').read_text())
        self.assertEqual(intent['state'], 'needs_resolution')
        self.assertEqual(intent['authority_references'], [])
        self.assertIsNone(intent['profile_authority_snapshot_digest'])
        self.assertIsNone(intent['catalog_revision'])


if __name__ == '__main__':
    unittest.main()
