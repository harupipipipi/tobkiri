"""Author unbound Named Profile intents from published composition declarations."""
from __future__ import annotations

import json
import argparse
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator
from referencing import Registry, Resource

ROOT = Path(__file__).parent
RUNTIME = ROOT.parents[2]


def validate_intent(intent: dict[str, Any]) -> None:
    """Validate only public source schema; this grants no runtime authority."""
    schemas = RUNTIME / 'tobkiri_protocol/schemas'
    registry = Registry()
    for path in schemas.glob('*.schema.json'):
        schema = json.loads(path.read_text())
        registry = registry.with_resource(schema['$id'], Resource.from_contents(schema))
    schema = json.loads((schemas / 'profile_intent_v1.schema.json').read_text())
    Draft202012Validator(schema, registry=registry).validate(intent)


def make_profile_intent(
    *, profile_id: str, display_name: str,
    base_definition: dict[str, Any], shell_definition: dict[str, Any],
    application_manifest: dict[str, Any], selected_pack_manifests: list[dict[str, Any]],
    platform: str = 'macos', architecture: str = 'arm64',
) -> dict[str, Any]:
    """Select published identities without inventing consumer edges or approvals."""
    application = application_manifest['pack']
    if application['kind'] != 'application':
        raise ValueError('explicit Application Pack required')
    identities = [application['id']] + [m['pack']['id'] for m in selected_pack_manifests]
    if len(identities) != len(set(identities)):
        raise ValueError('duplicate selected Pack identity')
    if not any(t['platform'] == platform and t['architecture'] == architecture
               for t in shell_definition['launch']['build_targets']):
        raise ValueError('published Shell build target required')
    intent = {
        'intent_api_version': 'io.tobkiri.profile-intent.v1',
        'profile_id': profile_id, 'display_name': display_name,
        'state': 'needs_resolution', 'mode': 'interactive', 'catalog_revision': None,
        'base': {'pack_id': base_definition['pack_id'], 'artifact_digest': None,
                 'definition_revision': None,
                 'resolution': 'verified_exact_artifact_required'},
        'shell': {'provider_id': shell_definition['provider_id'],
                  'pack_id': shell_definition['pack_id'], 'artifact_digest': None,
                  'executable_artifact_digest': None, 'definition_revision': None,
                  'contract_id': shell_definition['contract_id'],
                  'platform': platform, 'architecture': architecture},
        'packs': [{'pack_id': application['id'], 'artifact_digest': None,
                   'role': 'application'}] + [
                       {'pack_id': m['pack']['id'], 'artifact_digest': None,
                        'role': 'provider'} for m in selected_pack_manifests],
        'requested_edges': [], 'authority_references': [],
        'profile_authority_snapshot_digest': None,
    }
    validate_intent(intent)
    return intent


def write_named_intent() -> Path:
    """Write a new Tauri-targeted authoring intent using public bundle IDs."""
    bundle = RUNTIME / 'ecosystem/defaultspack/v4'
    intent = make_profile_intent(
        profile_id='independent.tauri.renderer',
        display_name='Tobkiri independent renderer',
        base_definition=json.loads((bundle / 'defaults-basepack.base.v1.json').read_text()),
        shell_definition=json.loads((bundle / 'shell.tauri.default.shell.v1.json').read_text()),
        application_manifest=json.loads((bundle / 'packs/runtime.tauri.application.default.pack.v4.json').read_text()),
        selected_pack_manifests=[json.loads((ROOT / 'acceptance.cli.presentation/pack.v4.json').read_text())],
    )
    output = ROOT / 'independent.tauri.renderer.profile.intent.v1.json'
    output.write_text(json.dumps(intent, sort_keys=True, indent=2) + '\n')
    return output


def compile_named_intent(destination: Path) -> Path:
    """Compile to a new owned source release via the formal public producer."""
    from core_runtime.profile_authoring import build_named_profile

    destination.resolve().relative_to(ROOT.resolve())
    intent = json.loads(write_named_intent().read_text())
    return build_named_profile(
        destination, template_bundle=RUNTIME / 'ecosystem/defaultspack/v4',
        intent=intent, additional_pack_roots=(ROOT / 'acceptance.cli.presentation',),
    )


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--compile-output', type=Path,
                        help='New directory inside this owned namespace')
    arguments = parser.parse_args()
    if arguments.compile_output is None:
        write_named_intent()
    else:
        compile_named_intent(arguments.compile_output)

