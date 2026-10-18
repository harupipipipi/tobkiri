"""Real generated audio contracts must support the typed voice editor."""
from pathlib import Path
import json

import pytest
from jsonschema import Draft202012Validator

from core_runtime.workflow_v4.binding import project_display_schema
from tobkiri_protocol.canonical import canonical_digest

ROOT = Path(__file__).resolve().parents[1]


def contracts():
    records = json.loads((ROOT / 'schemas/pack_v4_catalog.v1.json').read_text())['packs']
    return {c['contract_id']: c for p in records if p['pack_id'] in {'rumi_ai_modality_pack', 'rumi_ai_gateway_pack'} for c in p['provided_contracts']}


@pytest.mark.parametrize('contract,fields', [
    ('tobkiri.service.ai.audio.transcribe.v1', {'audio', 'model_profile_id'}),
    ('tobkiri.service.ai.text.generate.v1', {'text', 'model_profile_id'}),
    ('tobkiri.service.ai.audio.speech.v1', {'input', 'voice', 'model_profile_id'}),
])
def test_actual_catalog_exposes_typed_voice_ports_and_registered_model_selector(contract, fields):
    entry = contracts()[contract]
    schema = entry['schemas']['input']
    Draft202012Validator.check_schema(schema)
    projected = project_display_schema(schema)
    assert projected is not None
    assert fields <= set(projected['properties'])
    assert projected['properties']['model_profile_id']['x-tobkiri-selector'] == 'model-profile'
    pack = 'rumi_ai_gateway_pack' if '.text.' in contract else 'rumi_ai_modality_pack'
    generated = json.loads((ROOT / 'ecosystem' / pack / 'contracts.v4.json').read_text())
    actual = next(c for c in generated['contracts'] if c['contract_id'] == contract)
    assert actual['operations'][0]['input_schema_digest'] == canonical_digest(schema)


def test_audio_schemas_preserve_compatible_text_alias_but_reject_routing_overrides():
    speech = contracts()['tobkiri.service.ai.audio.speech.v1']['schemas']['input']
    validator = Draft202012Validator(speech)
    for payload in ({'input': 'hello'}, {'text': 'hello'}):
        assert validator.is_valid({**payload, 'model_profile_id': 'registered/model'})
    assert not validator.is_valid({'model_profile_id': 'registered/model'})
    for key in ('api_key', 'provider_id', 'endpoint', 'credential_handle'):
        assert not validator.is_valid({'input': 'hello', 'model_profile_id': 'registered/model', key: 'forbidden'})
    transcript = contracts()['tobkiri.service.ai.audio.transcribe.v1']['schemas']['output']
    assert 'text' in transcript['required']
    assert project_display_schema(transcript)['properties']['text']['type'] == 'string'
    output = contracts()['tobkiri.service.ai.audio.speech.v1']['schemas']['output']
    assert 'content' in output['required']
    assert output['properties']['content']['properties']['byte_size']['maximum'] == 1048576
